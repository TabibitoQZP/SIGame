# Neural vision reference

class FrameClient(AbstractContextManager["FrameClient"]):
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 300.0,
        http: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.http = http or requests.Session()
        self._owns_http = http is None
        if api_key is not None:
            self.http.headers["X-API-Key"] = api_key

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def _response(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.http.request(
            method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs
        )
        if not response.ok:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text[:500]
            raise FrameServiceError(f"HTTP {response.status_code}: {detail}")
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        data = self._response(method, path, **kwargs).json()
        if not isinstance(data, dict):
            raise FrameServiceError(f"expected JSON object, got {type(data).__name__}")
        return data

    def health(self) -> dict[str, Any]:
        return self._json("GET", "/healthz")

    def upload_image(
        self,
        image: bytes | str | Path,
        *,
        content_type: str | None = None,
    ) -> Frame:
        """Upload JPEG or PNG bytes in gateway-safe chunks."""
        if isinstance(image, (str, Path)):
            path = Path(image)
            data = path.read_bytes()
            content_type = content_type or {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
            }.get(path.suffix.lower())
        else:
            data = image
            if content_type is None:
                if data.startswith(b"\xff\xd8"):
                    content_type = "image/jpeg"
                elif data.startswith(b"\x89PNG\r\n\x1a\n"):
                    content_type = "image/png"
        if not data:
            raise ValueError("image is empty")
        if content_type not in {"image/jpeg", "image/png"}:
            raise ValueError("content_type must be image/jpeg or image/png")
        upload = self._json(
            "POST",
            "/v1/frames",
            json={
                "byte_count": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_type": content_type,
            },
        )
        frame_id = str(upload["frame_id"])
        chunk_bytes = int(upload["chunk_bytes"])
        try:
            if upload.get("protocol_version") != 2:
                raise FrameServiceError(
                    "frame service protocol mismatch: deploy the v2 single-frame server"
                )
            if not 0 < chunk_bytes <= 48 * 1024:
                raise FrameServiceError(f"invalid server chunk size: {chunk_bytes}")
            final_chunk: dict[str, Any] = {}
            for index, offset in enumerate(range(0, len(data), chunk_bytes)):
                final_chunk = self._json(
                    "PUT",
                    f"/v1/frames/{frame_id}/chunks/{index}",
                    data=data[offset : offset + chunk_bytes],
                    headers={"Content-Type": "application/octet-stream"},
                )
            return Frame(
                self,
                frame_id,
                width=int(final_chunk["width"]),
                height=int(final_chunk["height"]),
            )
        except Exception:
            try:
                self._json("DELETE", f"/v1/frames/{frame_id}")
            except (FrameServiceError, requests.RequestException):
                pass
            raise

    def upload_frame(
        self, frame: Any, *, image_format: str = "png", quality: int = 90
    ) -> Frame:
        """Encode a BGR uint8 ndarray as PNG, or opt into JPEG with image_format='jpeg'."""
        if image_format not in {"png", "jpeg"}:
            raise ValueError("image_format must be png or jpeg")
        if not 1 <= quality <= 100:
            raise ValueError("quality must be between 1 and 100")
        try:
            import cv2
        except ImportError:
            raise RuntimeError(
                "upload_frame requires cv2; upload_image accepts raw bytes"
            ) from None
        if (
            str(getattr(frame, "dtype", "")) != "uint8"
            or len(getattr(frame, "shape", ())) != 3
        ):
            raise ValueError("frame must be a BGR uint8 ndarray")
        if frame.shape[2] != 3:
            raise ValueError("frame must have three color channels")
        extension = ".png" if image_format == "png" else ".jpg"
        options = [] if image_format == "png" else [cv2.IMWRITE_JPEG_QUALITY, quality]
        ok, encoded = cv2.imencode(extension, frame, options)
        if not ok:
            raise ValueError("OpenCV could not encode frame")
        content_type = "image/png" if image_format == "png" else "image/jpeg"
        return self.upload_image(encoded.tobytes(), content_type=content_type)

    def download_result(self, descriptor: dict[str, Any]) -> bytes:
        result_id = str(descriptor["result_id"])
        byte_count = int(descriptor["byte_count"])
        chunk_bytes = int(descriptor["chunk_bytes"])
        if byte_count < 0 or not 0 < chunk_bytes <= 48 * 1024:
            raise FrameServiceError("invalid result descriptor")
        try:
            chunks = []
            for index in range((byte_count + chunk_bytes - 1) // chunk_bytes):
                chunk = self._response(
                    "GET", f"/v1/results/{result_id}/chunks/{index}"
                ).content
                if len(chunk) != min(chunk_bytes, byte_count - index * chunk_bytes):
                    raise FrameServiceError(
                        f"result chunk {index} has unexpected length"
                    )
                chunks.append(chunk)
            data = b"".join(chunks)
            if hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
                raise FrameServiceError("result SHA-256 mismatch")
            return data
        finally:
            try:
                self._json("DELETE", f"/v1/results/{result_id}")
            except (FrameServiceError, requests.RequestException):
                pass

    def _json_result(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(self.download_result(descriptor))
        if not isinstance(result, dict):
            raise FrameServiceError("expected a JSON result object")
        result["_meta"] = {
            key: descriptor[key]
            for key in ("model", "engine", "width", "height")
            if key in descriptor
        }
        return result
class Frame(AbstractContextManager["Frame"]):
    def __init__(
        self, client: FrameClient, frame_id: str, *, width: int, height: int
    ) -> None:
        self.client = client
        self.frame_id = frame_id
        self.width = width
        self.height = height
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.client._json("DELETE", f"/v1/frames/{self.frame_id}")
            self.closed = True

    def sam(
        self,
        *,
        text: str | None = None,
        points: list[list[float]] | None = None,
        point_labels: list[int] | None = None,
        bounding_boxes: list[list[float]] | None = None,
        bounding_box_labels: list[int] | None = None,
        obj_id: int | None = None,
        output_prob_thresh: float = 0.5,
    ) -> dict[str, Any]:
        """Segment one frame; all point and XYXY box coordinates are source pixels.

        Points are a separate SAM instance prompt. The server assigns request-local
        obj_id=1 when omitted and initializes the model before applying the points.
        """
        if points is not None:
            if text is not None or bounding_boxes is not None:
                raise ValueError(
                    "SAM point prompts cannot be combined with text or boxes"
                )
            if not points or not point_labels or 1 not in point_labels:
                raise ValueError("SAM point prompts need at least one positive point")
            if obj_id is None:
                obj_id = 1
        body = {
            "text": text,
            "points": points,
            "point_labels": point_labels,
            "bounding_boxes": bounding_boxes,
            "bounding_box_labels": bounding_box_labels,
            "obj_id": obj_id,
            "output_prob_thresh": output_prob_thresh,
        }
        descriptor = self.client._json(
            "POST",
            f"/v1/frames/{self.frame_id}/sam",
            json={key: value for key, value in body.items() if value is not None},
        )
        return self.client._json_result(descriptor)

    def depth_npz(self, *, process_res: int = 504) -> bytes:
        """Return compressed NPZ bytes containing depth and available camera fields."""
        _, payload = self.depth_npz_with_metadata(process_res=process_res)
        return payload

    def depth_npz_with_metadata(
        self, *, process_res: int = 504
    ) -> tuple[dict[str, Any], bytes]:
        """Return the result descriptor and compressed NPZ bytes."""
        descriptor = self.client._json(
            "POST",
            f"/v1/frames/{self.frame_id}/depth",
            json={"process_res": process_res},
        )
        return descriptor, self.client.download_result(descriptor)

    def depth(self, *, process_res: int = 504) -> dict[str, Any]:
        """Return NumPy arrays; requires numpy only when this method is called."""
        try:
            import numpy as np
        except ImportError:
            raise RuntimeError(
                "depth requires numpy; depth_npz returns raw bytes"
            ) from None
        descriptor, payload = self.depth_npz_with_metadata(process_res=process_res)
        with np.load(io.BytesIO(payload), allow_pickle=False) as data:
            arrays = {key: data[key] for key in data.files}
        arrays["_meta"] = {
            key: descriptor[key]
            for key in (
                "model",
                "depth_units",
                "width",
                "height",
                "shape",
                "native_shape",
                "interpolation",
                "dtype",
            )
            if key in descriptor
        }
        return arrays

    def ocr(self) -> dict[str, Any]:
        descriptor = self.client._json("POST", f"/v1/frames/{self.frame_id}/ocr")
        return self.client._json_result(descriptor)
