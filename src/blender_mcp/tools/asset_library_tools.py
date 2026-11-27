"""
Portmanteau MCP tools for Poly Haven (public API) and Sketchfab (token).

Complements ahujasid/blender-mcp-style workflows without requiring their TCP add-on.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Literal

from blender_mcp.app import get_app

_MAX_UNZIP_BYTES = 500 * 1024 * 1024  # BLENDER-011: 500 MiB decompression cap


def _safe_extractall(zf: zipfile.ZipFile, dest_dir: Path) -> None:
    """Extract zip while tracking actual written bytes to prevent ZIP bombs."""
    total = 0
    for member in zf.infolist():
        with zf.open(member) as src:
            dest = dest_dir / member.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            if member.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                with open(dest, "wb") as dst:
                    while chunk := src.read(65536):
                        total += len(chunk)
                        if total > _MAX_UNZIP_BYTES:
                            raise ValueError("ZIP decompression limit exceeded (500 MiB)")
                        dst.write(chunk)


from blender_mcp.handlers.asset_repository_handler import import_blend_asset, import_scene_asset
from blender_mcp.services.ambientcg_api import (
    AmbientCGAPI,
    build_pbr_material_script,
    resolve_download_url,
)
from blender_mcp.services.asset_fetch import download_entry_list, download_single
from blender_mcp.services.polyhaven_api import (
    PolyHavenAPI,
    resolve_hdri_url,
    resolve_model_import_bundle,
    resolve_texture_blend_bundle,
)
from blender_mcp.services.sketchfab_api import SketchfabAPI

logger = logging.getLogger(__name__)

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_MUTATING = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}


def _hdr_world_script(hdr_path: str) -> str:
    """Generate bpy script: load HDRI and wire Environment Texture to World output."""
    fp = hdr_path.replace("\\", "/")
    return f"""
import bpy
fp = r"{fp}"
img = bpy.data.images.load(fp)
img.name = "PH_HDRI_env"
scene = bpy.context.scene
world = scene.world
if world is None:
    world = bpy.data.worlds.new("PH_World")
    scene.world = world
world.use_nodes = True
nt = world.node_tree
nt.nodes.clear()
out = nt.nodes.new("ShaderNodeOutputWorld")
out.location = (400, 0)
bg = nt.nodes.new("ShaderNodeBackground")
bg.location = (200, 0)
env = nt.nodes.new("ShaderNodeTexEnvironment")
env.location = (0, 0)
env.image = img
nt.links.new(env.outputs["Color"], bg.inputs["Color"])
nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
print("SUCCESS: HDRI world configured")
"""


def _register_asset_library_tools() -> None:
    app = get_app()

    @app.tool(annotations=_MUTATING)
    async def manage_asset_library(
        operation: Literal[
            "polyhaven_categories",
            "polyhaven_search",
            "polyhaven_import",
            "hdri_setup",
            "ambientcg_search",
            "ambientcg_import",
            "sketchfab_search",
            "sketchfab_import",
            "info",
        ] = "info",
        # Poly Haven
        asset_type: str = "hdris",
        categories: str | None = None,
        page: int = 1,
        asset_id: str = "",
        resolution: str = "4k",
        file_format: str | None = None,
        model_prefer: str = "gltf",
        # Sketchfab
        query: str = "",
        sketchfab_uid: str = "",
        sketchfab_token: str | None = None,
        search_limit: int = 24,
        downloadable_only: bool = True,
    ) -> dict[str, Any]:
        """
        PORTMANTEAU: Poly Haven + Sketchfab asset discovery and import (fleet subprocess Blender).

        Operations:
        - info: Capabilities and env vars (SKETCHFAB_API_TOKEN).
        - polyhaven_categories: List category counts for hdris | textures | models.
        - polyhaven_search: Search catalog (uses api.polyhaven.com/assets).
        - polyhaven_import: Download + import — HDRIs set as world env; textures/models via files API bundles.
        - hdri_setup: One-shot: search Poly Haven HDRIs by query, pick best match, download + wire as world env.
        - ambientcg_search: Search ambientCG for free CC0 PBR materials (no key required).
        - ambientcg_import: Download + import an ambientCG material as a Principled BSDF node setup.
        - sketchfab_search: Sketchfab v3 search (requires token).
        - sketchfab_import: Download + import glTF from Sketchfab (requires token + downloadable model).

        Args:
            operation: Which operation to run.
            asset_type: For Poly Haven: hdris, textures, models, or all (search).
            categories: Optional comma-separated Poly Haven categories filter.
            page: Results page for polyhaven_search.
            asset_id: Poly Haven asset slug (e.g. venice_sunset) or Sketchfab UID for import.
            resolution: 1k | 2k | 4k | 8k | 16k where applicable.
            file_format: For HDRIs: hdr | exr. For models: gltf | fbx | blend preference chain start.
            model_prefer: Preferred model package: gltf, fbx, or blend.
            query: Sketchfab search query.
            sketchfab_uid: Model UID for sketchfab_import.
            sketchfab_token: Optional; defaults to SKETCHFAB_API_TOKEN env.
            search_limit: Max Sketchfab results (1-100).
            downloadable_only: Sketchfab search filter.

        Returns:
            Structured dict with success, message, and operation-specific fields.

        ## Return Format
        Standard dict with keys: success, message, data

        ## Examples
        ```python
        await call_tool("manage_asset_library", {"operation": "polyhaven_search", "asset_type": "hdris"})
        ```
        """
        if operation == "info":
            return {
                "success": True,
                "message": "manage_asset_library: Poly Haven uses public API (no key). "
                "Sketchfab search/download need SKETCHFAB_API_TOKEN or sketchfab_token.",
                "env": {"SKETCHFAB_API_TOKEN": bool(os.environ.get("SKETCHFAB_API_TOKEN"))},
            }

        api = PolyHavenAPI()

        if operation == "polyhaven_categories":
            data = await api.categories(asset_type)
            return {"success": True, "categories": data}

        if operation == "polyhaven_search":
            raw = await api.search_assets(asset_type=asset_type, categories=categories, page=page)
            if not isinstance(raw, dict):
                return {"success": False, "error": "Unexpected Poly Haven response"}
            items: list[dict[str, Any]] = []
            for aid, meta in raw.items():
                if not isinstance(meta, dict):
                    continue
                items.append(
                    {
                        "id": aid,
                        "name": meta.get("name", aid),
                        "type": meta.get("type"),
                        "categories": meta.get("categories", []),
                    }
                )
            items.sort(key=lambda x: str(x.get("name", "")))
            return {
                "success": True,
                "page": page,
                "total_returned": len(items),
                "assets": items[:200],
            }

        if operation == "polyhaven_import":
            if not asset_id.strip():
                return {"success": False, "error": "asset_id is required"}
            from blender_mcp.utils.blender_executor import get_blender_executor

            executor = get_blender_executor()
            files_data = await api.files(asset_id.strip())
            at = asset_type.lower().strip()

            if at == "hdris":
                url = resolve_hdri_url(files_data, resolution, file_format or "hdr")
                if not url:
                    return {"success": False, "error": "Could not resolve HDRI URL from API"}
                tdir = tempfile.mkdtemp(prefix="ph_hdri_")
                try:
                    hdr_path = Path(tdir) / f"{asset_id}.hdr"
                    if url.lower().endswith(".exr"):
                        hdr_path = Path(tdir) / f"{asset_id}.exr"
                    await download_single(url, hdr_path)
                    script = _hdr_world_script(str(hdr_path.resolve()))
                    out = await executor.execute_script(script)
                    return {
                        "success": True,
                        "message": "HDRI loaded and world nodes configured",
                        "local_file": str(hdr_path),
                        "blender_output_tail": out[-2000:],
                    }
                except BaseException:
                    # Failed fetch: do not leave the partial download behind.
                    shutil.rmtree(tdir, ignore_errors=True)
                    raise

            if at == "textures":
                entries, primary = resolve_texture_blend_bundle(files_data, resolution)
                if not primary or not entries:
                    return {"success": False, "error": "Could not resolve texture blend bundle"}
                tdir = Path(tempfile.mkdtemp(prefix="ph_tex_"))
                try:
                    await download_entry_list(entries, tdir)
                    primary_path = tdir / Path(primary).name
                    if not primary_path.is_file():
                        blend_files = list(tdir.rglob("*.blend"))
                        primary_path = blend_files[0] if blend_files else primary_path
                    ext = primary_path.suffix.lower().lstrip(".")
                    result = await import_scene_asset(str(primary_path.resolve()), ext, {})
                    result["success"] = result.get("status") == "SUCCESS" or result.get("success", True)
                    result["local_file"] = str(primary_path)
                    return result
                except BaseException:
                    # Failed fetch: do not leave the partial download behind.
                    shutil.rmtree(tdir, ignore_errors=True)
                    raise

            if at == "models":
                prefer = (file_format or model_prefer or "gltf").lower()
                if prefer not in ("gltf", "fbx", "blend"):
                    prefer = "gltf"
                entries, primary_url = resolve_model_import_bundle(files_data, resolution, prefer)
                if not entries or not primary_url:
                    return {"success": False, "error": "Could not resolve model bundle"}
                tdir = Path(tempfile.mkdtemp(prefix="ph_model_"))
                try:
                    await download_entry_list(entries, tdir)
                    main_files = list(tdir.rglob("*.gltf")) + list(tdir.rglob("*.glb"))
                    fbx_files = list(tdir.rglob("*.fbx"))
                    blend_files = list(tdir.rglob("*.blend"))
                    target: Path | None = None
                    import_ext = "gltf"
                    if main_files:
                        target = main_files[0]
                    elif fbx_files:
                        target = fbx_files[0]
                        import_ext = "fbx"
                    elif blend_files:
                        target = blend_files[0]
                        import_ext = "blend"
                    if target is None:
                        return {"success": False, "error": "No importable file found after download"}
                    if import_ext == "blend":
                        r = await import_blend_asset(str(target.resolve()), {})
                    else:
                        r = await import_scene_asset(str(target.resolve()), import_ext, {})
                    r["success"] = r.get("status") == "SUCCESS" or r.get("success", True)
                    r["local_dir"] = str(tdir)
                    r["imported_file"] = str(target)
                    return r
                except BaseException:
                    # Failed fetch: do not leave the partial download behind.
                    shutil.rmtree(tdir, ignore_errors=True)
                    raise

            return {"success": False, "error": f"Unsupported asset_type for import: {at}"}

        if operation == "hdri_setup":
            # One-shot: search → pick top result → download + wire as world environment.
            search_query = query.strip() or "studio"
            raw = await api.search_assets(asset_type="hdris", page=1)
            if not isinstance(raw, dict):
                return {"success": False, "error": "Poly Haven search failed"}
            # Filter by query terms (Poly Haven /assets has no server-side text search)
            terms = search_query.lower().split()
            ranked: list[tuple[int, str, dict[str, Any]]] = []
            for aid, meta in raw.items():
                score = sum(1 for t in terms if t in aid.lower() or t in str(meta.get("tags", [])).lower())
                ranked.append((score, aid, meta))
            ranked.sort(key=lambda x: (-x[0], x[1]))
            if not ranked:
                return {"success": False, "error": "No HDRIs found on Poly Haven"}
            chosen_id = ranked[0][1]
            chosen_meta = ranked[0][2]
            from blender_mcp.utils.blender_executor import get_blender_executor

            executor = get_blender_executor()
            files_data = await api.files(chosen_id)
            url = resolve_hdri_url(files_data, resolution, file_format or "hdr")
            if not url:
                return {"success": False, "error": f"Could not resolve HDRI URL for '{chosen_id}'"}
            tdir = tempfile.mkdtemp(prefix="ph_hdri_")
            try:
                ext = ".exr" if url.lower().endswith(".exr") else ".hdr"
                hdr_path = Path(tdir) / f"{chosen_id}{ext}"
                await download_single(url, hdr_path)
                script = _hdr_world_script(str(hdr_path.resolve()))
                out = await executor.execute_script(script)
                return {
                    "success": True,
                    "message": f"HDRI '{chosen_id}' loaded as world environment",
                    "asset_id": chosen_id,
                    "query": search_query,
                    "name": chosen_meta.get("name", chosen_id),
                    "tags": chosen_meta.get("tags", []),
                    "blender_output_tail": out[-500:],
                }
            except BaseException:
                shutil.rmtree(tdir, ignore_errors=True)
                raise

        if operation == "ambientcg_search":
            acg = AmbientCGAPI()
            results = await acg.search(query=query, limit=min(max(1, search_limit), 50))
            return {"success": True, "total_returned": len(results), "assets": results}

        if operation == "ambientcg_import":
            if not asset_id.strip():
                return {"success": False, "error": "asset_id is required (e.g. 'Concrete034')"}
            acg = AmbientCGAPI()
            asset = await acg.asset_info(asset_id.strip())
            dl_url, res_key = resolve_download_url(asset, resolution)
            tdir = Path(tempfile.mkdtemp(prefix="acg_"))
            try:
                zip_path = tdir / f"{asset_id}.zip"
                await download_single(dl_url, zip_path)
                extract_dir = tdir / "extracted"
                extract_dir.mkdir()
                with zipfile.ZipFile(zip_path, "r") as zf:
                    _safe_extractall(zf, extract_dir)
                # Map suffix → file path
                from blender_mcp.services.ambientcg_api import TEXTURE_MAP

                texture_files: dict[str, str] = {}
                for f in extract_dir.rglob("*"):
                    if not f.is_file():
                        continue
                    stem = f.stem  # e.g. Concrete034_2K_Color
                    for suffix in TEXTURE_MAP:
                        if stem.endswith(suffix):
                            texture_files[suffix] = str(f.resolve())
                            break
                if not texture_files:
                    return {"success": False, "error": "No recognisable texture maps found in download"}
                from blender_mcp.utils.blender_executor import get_blender_executor

                executor = get_blender_executor()
                mat_name = asset.get("assetId", asset_id)
                script = build_pbr_material_script(mat_name, texture_files)
                result = await executor.execute_script(script)
                ok = "AMBIENTCG_MAT_OK:" in (result or "")
                return {
                    "success": ok,
                    "message": f"ambientCG material '{mat_name}' created"
                    if ok
                    else "Script ran but material status unclear",
                    "material_name": mat_name,
                    "resolution": res_key,
                    "maps_loaded": sorted(texture_files.keys()),
                    "blender_output_tail": (result or "")[-500:],
                }
            except BaseException:
                shutil.rmtree(tdir, ignore_errors=True)
                raise

        if operation == "sketchfab_search":
            token = sketchfab_token or os.environ.get("SKETCHFAB_API_TOKEN")
            if not token:
                return {
                    "success": False,
                    "error": "Set SKETCHFAB_API_TOKEN or pass sketchfab_token",
                }
            sf = SketchfabAPI(api_token=token)
            data = await sf.search_models(
                query=query or "chair",
                categories=None,
                count=min(max(1, search_limit), 100),
                downloadable=downloadable_only,
            )
            results = data.get("results") or []
            slim = [
                {
                    "uid": r.get("uid"),
                    "name": r.get("name"),
                    "vertex_count": r.get("vertexCount"),
                    "is_downloadable": r.get("isDownloadable"),
                }
                for r in results
                if isinstance(r, dict)
            ]
            return {"success": True, "models": slim, "raw_count": len(results)}

        if operation == "sketchfab_import":
            uid = (sketchfab_uid or asset_id).strip()
            if not uid:
                return {"success": False, "error": "sketchfab_uid (or asset_id) is required"}
            token = sketchfab_token or os.environ.get("SKETCHFAB_API_TOKEN")
            if not token:
                return {"success": False, "error": "Set SKETCHFAB_API_TOKEN or pass sketchfab_token"}
            sf = SketchfabAPI(api_token=token)
            info = await sf.download_info(uid)
            gltf_block = info.get("gltf") or info.get("glTF") or {}
            url = None
            if isinstance(gltf_block, dict):
                url = gltf_block.get("url")
            if not url:
                return {"success": False, "error": f"No glTF download URL in API response: {json.dumps(info)[:500]}"}
            tdir = tempfile.mkdtemp(prefix="sf_dl_")
            try:
                url_path = Path(url.split("?", 1)[0])
                suffix = url_path.suffix.lower() or ".bin"
                dl_path = Path(tdir) / f"download{suffix}"
                await download_single(url, dl_path)

                target: Path | None = None
                extract_dir = Path(tdir) / "extracted"

                if dl_path.suffix.lower() in (".glb", ".gltf"):
                    target = dl_path
                elif zipfile.is_zipfile(dl_path):
                    extract_dir.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(dl_path, "r") as zf:
                        _safe_extractall(zf, extract_dir)
                    glbs = list(extract_dir.rglob("*.glb"))
                    gltfs = list(extract_dir.rglob("*.gltf"))
                    target = glbs[0] if glbs else (gltfs[0] if gltfs else None)
                else:
                    try:
                        extract_dir.mkdir(parents=True, exist_ok=True)
                        with zipfile.ZipFile(dl_path, "r") as zf:
                            _safe_extractall(zf, extract_dir)
                        glbs = list(extract_dir.rglob("*.glb"))
                        gltfs = list(extract_dir.rglob("*.gltf"))
                        target = glbs[0] if glbs else (gltfs[0] if gltfs else None)
                    except zipfile.BadZipFile:
                        with dl_path.open("rb") as f:
                            head = f.read(16)
                        if len(head) >= 8 and head[4:8] == b"glTF":
                            target = dl_path

                if target is None:
                    return {
                        "success": False,
                        "error": "Download was not a zip with .glb/.gltf nor a raw glTF file",
                        "local_file": str(dl_path),
                    }
                ext = target.suffix.lower().lstrip(".")
                result = await import_scene_asset(str(target.resolve()), ext, {})
                result["success"] = result.get("status") == "SUCCESS" or result.get("success", True)
                result["source_uid"] = uid
                result["imported_file"] = str(target)
                return result
            except BaseException:
                # Failed fetch: do not leave the partial download behind.
                shutil.rmtree(tdir, ignore_errors=True)
                raise

        return {"success": False, "error": f"Unknown operation: {operation}"}


_register_asset_library_tools()
