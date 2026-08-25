from app.plugins import PluginBase, Menu, MountPoint
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from app.plugins.worker import run_function_async
import json
import logging

logger = logging.getLogger(__name__)


def _job(task_id, project_id, plugin_path, python_packages_path, params, progress_callback=None):
    # This function must be self-contained: WebODM serializes only its source
    # and evaluates it in an isolated Celery worker namespace.
    import logging, os, pathlib, shutil, site, sys
    logger = logging.getLogger("orthoswift.webodm.worker")
    priority_paths = []
    for base in [python_packages_path, plugin_path]:
        if base:
            p = str(base)
            sp = os.path.join(p, "site-packages")
            if os.path.isdir(sp) and sp not in priority_paths:
                priority_paths.append(sp)
            if os.path.isdir(p) and p not in priority_paths:
                priority_paths.append(p)
    for p in reversed(priority_paths):
        site.addsitedir(p)
        while p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    try:
        import shapely
        for modname in ("strtree", "ops", "geometry", "affinity", "wkt", "prepared"):
            try:
                mod = __import__(f"shapely.{modname}", fromlist=[modname])
                if not hasattr(shapely, modname):
                    setattr(shapely, modname, mod)
            except Exception:
                pass
    except Exception:
        pass
    try:
        import rasterio
        from app.models import Task
        task = Task.objects.get(pk=task_id, project_id=project_id)
        task_dir = pathlib.Path(task.task_path()).resolve()
        if progress_callback:
            progress_callback("Locating and validating multispectral orthophoto", 5)

        candidates, seen = [], set()
        def add(path):
            candidate = pathlib.Path(path)
            key = str(candidate)
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)
        for parts in (
            ("odm_orthophoto", "odm_orthophoto.tif"),
            ("odm_orthophoto.tif",),
            ("odm_orthophoto", "odm_orthophoto.original.tif"),
        ):
            try:
                add(task.assets_path(*parts))
            except Exception as exc:
                logger.debug("WebODM assets_path candidate failed for %s: %s", parts, exc)
        for path in (
            task_dir / "assets" / "odm_orthophoto" / "odm_orthophoto.tif",
            task_dir / "assets" / "odm_orthophoto.tif",
            task_dir / "odm_orthophoto" / "odm_orthophoto.tif",
            task_dir / "odm_orthophoto.tif",
            task_dir / "assets" / "odm_orthophoto" / "odm_orthophoto.original.tif",
            task_dir / "odm_orthophoto" / "odm_orthophoto.original.tif",
        ):
            add(path)
        for root in (task_dir / "assets", task_dir / "odm_orthophoto"):
            if root.is_dir():
                for path in sorted(root.glob("**/*orthophoto*.tif")):
                    add(path)
        candidates = [path for path in candidates if path.is_file()]
        if not candidates:
            raise FileNotFoundError(
                "No task-local ODM orthophoto GeoTIFF was found. Complete the WebODM "
                "task with orthophoto generation enabled, then retry."
            )

        compatible, inspected, errors = [], [], []
        for path in candidates:
            try:
                with rasterio.open(path) as dataset:
                    info = {
                        "path": path, "name": path.name, "bands": int(dataset.count),
                        "width": int(dataset.width), "height": int(dataset.height),
                        "crs": str(dataset.crs) if dataset.crs else None,
                        "descriptions": [value or "" for value in dataset.descriptions],
                    }
                    if dataset.driver == "GTiff" and dataset.crs is not None:
                        inspected.append(info)
                        compatible.append(info)
                    else:
                        errors.append(f"{path.name}: not a georeferenced GeoTIFF")
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
        if not compatible:
            found = "; ".join(f"{item['name']} ({item['bands']} bands)" for item in inspected) or "; ".join(errors)
            raise ValueError(f"No valid georeferenced task orthophoto was found. Inspected: {found}")
        compatible.sort(key=lambda item: (
            item["name"] != "odm_orthophoto.tif", -item["bands"], len(str(item["path"]))
        ))
        raster = compatible[0]
        ortho = raster["path"]
        out = task_dir / "orthoswift"
        if out.exists():
            shutil.rmtree(out)
        try:
            from orthoswift.runner import run
        except ImportError:
            from runner import run
        config = {
            "out_dir": str(out), "orthomosaic_path": str(ortho),
            "zones": params["zones"],
            "offline_basemap": params["offline_basemap"],
            "fertilizer_rate_plan": params.get("fertilizer_rate_plan"),
            "spot_spray_rate_plan": params.get("spot_spray_rate_plan"),
            "host": {"name": "WebODM", "task_id": str(task_id), "project_id": str(project_id)},
        }
        if progress_callback:
            progress_callback(f"Analyzing {raster['name']} ({raster['bands']} bands)", 15)
        result = run(config, progress_callback=progress_callback)
        public_raster = {key: value for key, value in raster.items() if key != "path"}
        return {"file": result["archive"], "filename": "orthoswift-deliverables.zip",
                "input_raster": public_raster}
    except Exception as exc:
        logger.exception("OrthoSWIFT WebODM worker failed")
        return {"error": f"{type(exc).__name__}: {exc}", "error_type": type(exc).__name__}


def _job_file(file_path_str, plugin_path, python_packages_path, params, progress_callback=None):
    import logging, os, pathlib, site, sys, zipfile
    logger = logging.getLogger("orthoswift.webodm.worker")
    priority_paths = []
    for base in [python_packages_path, plugin_path]:
        if base:
            p = str(base)
            sp = os.path.join(p, "site-packages")
            if os.path.isdir(sp) and sp not in priority_paths:
                priority_paths.append(sp)
            if os.path.isdir(p) and p not in priority_paths:
                priority_paths.append(p)
    for p in reversed(priority_paths):
        site.addsitedir(p)
        while p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    try:
        import shapely
        for modname in ("strtree", "ops", "geometry", "affinity", "wkt", "prepared"):
            try:
                mod = __import__(f"shapely.{modname}", fromlist=[modname])
                if not hasattr(shapely, modname):
                    setattr(shapely, modname, mod)
            except Exception:
                pass
    except Exception:
        pass
    try:
        import rasterio
        input_path = pathlib.Path(file_path_str).resolve()
        if not input_path.is_file():
            raise FileNotFoundError(f"Uploaded file not found: {input_path}")
        
        work_dir = pathlib.Path(params.get("temp_dir") or input_path.parent).resolve()
        ortho_path = None
        
        if progress_callback:
            progress_callback("Validating uploaded file", 5)

        # Handle ZIP archive (e.g. all.zip)
        if input_path.suffix.lower() == ".zip":
            if progress_callback:
                progress_callback("Extracting orthophoto from ZIP archive", 10)
            extract_dir = work_dir / "extracted"
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(input_path, "r") as zf:
                infos = [info for info in zf.infolist()
                         if info.filename.lower().endswith((".tif", ".tiff"))
                         and not info.filename.startswith("__MACOSX")
                         and not info.is_dir()]
                if not infos:
                    raise ValueError("No GeoTIFF orthomosaic (.tif) found inside the uploaded ZIP archive.")
                infos.sort(key=lambda info: (
                    not ("orthophoto" in info.filename.lower() or "ortho" in info.filename.lower()),
                    len(info.filename),
                ))
                selected = infos[0]
                if selected.file_size > 20 * 1024 * 1024 * 1024:
                    raise ValueError("The selected orthomosaic exceeds the 20 GiB extraction limit.")
                if selected.compress_size == 0 and selected.file_size > 0:
                    raise ValueError("The selected ZIP member has an invalid compression size.")
                if selected.compress_size and selected.file_size / selected.compress_size > 1000:
                    raise ValueError("The selected ZIP member has an unsafe compression ratio.")
                # Never extract a user-controlled member path. Stream into a
                # fixed basename inside the per-request working directory.
                suffix = pathlib.Path(selected.filename).suffix.lower()
                ortho_path = extract_dir / f"orthomosaic{suffix}"
                written = 0
                with zf.open(selected, "r") as source, ortho_path.open("wb") as destination:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > 20 * 1024 * 1024 * 1024:
                            raise ValueError("The extracted orthomosaic exceeds the 20 GiB limit.")
                        destination.write(chunk)
        elif input_path.suffix.lower() in (".tif", ".tiff"):
            ortho_path = input_path
        else:
            raise ValueError(f"Unsupported file format '{input_path.suffix}'. Please upload a .tif orthomosaic or .zip archive.")

        # Validate GeoTIFF
        with rasterio.open(ortho_path) as dataset:
            info = {
                "name": ortho_path.name, "bands": int(dataset.count),
                "width": int(dataset.width), "height": int(dataset.height),
                "crs": str(dataset.crs) if dataset.crs else None,
            }
            if dataset.driver != "GTiff" or not dataset.crs:
                raise ValueError(f"File {ortho_path.name} is not a valid georeferenced GeoTIFF.")

        out = work_dir / "orthoswift"
        try:
            from orthoswift.runner import run
        except ImportError:
            from runner import run
        config = {
            "out_dir": str(out),
            "orthomosaic_path": str(ortho_path),
            "zones": params.get("zones", 3),
            "offline_basemap": params.get("offline_basemap", True),
            "fertilizer_rate_plan": params.get("fertilizer_rate_plan"),
            "spot_spray_rate_plan": params.get("spot_spray_rate_plan"),
            "host": {"name": "WebODM", "source": "uploaded_file"},
        }
        if progress_callback:
            progress_callback(f"Analyzing {ortho_path.name} ({info['bands']} bands)", 20)
        result = run(config, progress_callback=progress_callback)
        return {"file": result["archive"], "filename": "orthoswift-deliverables.zip", "input_raster": info}
    except Exception as exc:
        logger.exception("OrthoSWIFT WebODM worker failed on uploaded file")
        return {"error": f"{type(exc).__name__}: {exc}", "error_type": type(exc).__name__}


def _save_uploaded_file(uploaded, dest_path, max_bytes=20 * 1024 * 1024 * 1024):
    """Save a Django uploaded-file object to dest_path.

    Django exposes two concrete upload types:
      - TemporaryUploadedFile  — backed by a real temp file on disk.
        By the time we get here the SpooledTemporaryFile may already be
        closed, so seeking raises "seek of closed file".  We copy the file
        directly from its path instead of touching the stream.
      - InMemoryUploadedFile   — backed by a BytesIO buffer.
        The .open() guard raises "The file cannot be reopened." if called
        twice, so we seek the buffer to 0 instead.
    """
    import pathlib, shutil
    dest_path = pathlib.Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    declared_size = getattr(uploaded, "size", None)
    if declared_size is not None and int(declared_size) > max_bytes:
        raise ValueError("Uploaded file exceeds the 20 GiB limit")

    # ── TemporaryUploadedFile: copy from disk, never touch the stream ──────
    if hasattr(uploaded, "temporary_file_path"):
        src = pathlib.Path(uploaded.temporary_file_path())
        if src.stat().st_size > max_bytes:
            raise ValueError("Uploaded file exceeds the 20 GiB limit")
        try:
            shutil.copy2(src, dest_path)
        except Exception:
            dest_path.unlink(missing_ok=True)
            raise
        return

    # ── InMemoryUploadedFile (BytesIO): seek to 0 then stream ─────────────
    written = 0
    try:
        file_obj = getattr(uploaded, "file", None)
        if file_obj is not None and hasattr(file_obj, "seek"):
            try:
                file_obj.seek(0)
            except (ValueError, OSError):
                pass
        elif hasattr(uploaded, "seek"):
            try:
                uploaded.seek(0)
            except (ValueError, OSError):
                pass

        chunks = (
            uploaded.chunks()
            if hasattr(uploaded, "chunks")
            else iter(lambda: uploaded.read(1024 * 1024), b"")
        )
        with dest_path.open("wb") as destination:
            for chunk in chunks:
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("Uploaded file exceeds the 20 GiB limit")
                destination.write(chunk)
    except Exception:
        dest_path.unlink(missing_ok=True)
        raise


class Plugin(PluginBase):
    def main_menu(self):
        return [Menu(_("OrthoSWIFT"), self.public_url(""), "fa fa-leaf fa-fw")]

    def app_mount_points(self):
        @login_required
        def home(request):
            return render(request, self.template_path("index.html"), {})
        return [MountPoint('$', home)]

    def api_mount_points(self):
        @login_required
        def start(request):
            from app.models import Task
            from app.api.common import check_project_perms
            import pathlib, tempfile, uuid
            if request.method != "POST":
                return JsonResponse({"error": "POST required"}, status=405)
            try:
                zones = 3
                offline_basemap = True
                
                # Check for uploaded file in multipart form-data
                if request.FILES:
                    uploaded = request.FILES.get("orthomosaic_file") or request.FILES.get("file")
                    if not uploaded:
                        # Grab the first file uploaded if name differs
                        uploaded = next(iter(request.FILES.values()), None)
                    if uploaded:
                        upload_id = str(uuid.uuid4())
                        temp_dir = pathlib.Path(tempfile.gettempdir()) / f"orthoswift_upload_{upload_id}"
                        temp_dir.mkdir(parents=True, exist_ok=True)
                        safe_name = pathlib.Path(str(uploaded.name)).name
                        if not safe_name or safe_name in {".", ".."}:
                            raise ValueError("Invalid uploaded filename")
                        saved_path = temp_dir / safe_name
                        _save_uploaded_file(uploaded, saved_path)

                        zones = int(request.POST.get("zones", 3))
                        if not 2 <= zones <= 8:
                            raise ValueError("zones must be between 2 and 8")
                        raw_basemap = str(request.POST.get("offline_basemap", "true")).strip().lower()
                        offline_basemap = raw_basemap in {"1", "true", "yes", "on"}

                        fertilizer_rate_plan = None
                        raw_plan = request.POST.get("fertilizer_rate_plan", "")
                        if raw_plan:
                            try:
                                fertilizer_rate_plan = json.loads(raw_plan)
                            except json.JSONDecodeError as exc:
                                raise ValueError("fertilizer_rate_plan must be valid JSON") from exc

                        spot_spray_rate_plan = None
                        raw_spot_plan = request.POST.get("spot_spray_rate_plan", "")
                        if raw_spot_plan:
                            try:
                                spot_spray_rate_plan = json.loads(raw_spot_plan)
                            except json.JSONDecodeError as exc:
                                raise ValueError("spot_spray_rate_plan must be valid JSON") from exc

                        params = {
                            "zones": zones,
                            "offline_basemap": offline_basemap,
                            "uploaded_file_path": str(saved_path),
                            "temp_dir": str(temp_dir),
                            "fertilizer_rate_plan": fertilizer_rate_plan,
                            "spot_spray_rate_plan": spot_spray_rate_plan,
                        }
                        worker = run_function_async(
                            _job_file, str(saved_path), self.get_path(),
                            self.get_python_packages_path(), params, with_progress=True,
                        )
                        return JsonResponse({"celery_task_id": worker.task_id})
                
                # Fallback for JSON body if task_id/project_id passed
                content_type = request.META.get("CONTENT_TYPE", "")
                if "application/json" in content_type:
                    data = json.loads(request.body.decode("utf-8"))
                    task = Task.objects.get(pk=data["task_id"], project_id=data["project_id"])
                    check_project_perms(request, task.project, ("change_project",))
                    params = {
                        "zones": int(data.get("zones", 3)),
                        "offline_basemap": data.get("offline_basemap", True) is not False,
                        "fertilizer_rate_plan": data.get("fertilizer_rate_plan"),
                        "spot_spray_rate_plan": data.get("spot_spray_rate_plan"),
                    }
                    worker = run_function_async(
                        _job, str(task.id), str(task.project_id), self.get_path(),
                        self.get_python_packages_path(), params, with_progress=True,
                    )
                    return JsonResponse({"celery_task_id": worker.task_id})
                
                return JsonResponse({"error": "Please select a GeoTIFF (.tif) or WebODM all.zip archive to upload."}, status=400)
            except Exception as exc:
                logger.exception("Failed starting OrthoSWIFT processing")
                return JsonResponse({"error": str(exc)}, status=400)
        return [MountPoint('run$', start)]

    def include_css_files(self):
        return ['style.css']

    def include_js_files(self):
        return ['main.js']
