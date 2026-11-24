"""
ambientCG public API (https://ambientcg.com/api/v2).

Provides free CC0 PBR materials (albedo, normal, roughness, metalness, AO, displacement).
No API key required.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://ambientcg.com/api/v2/full_json"
DEFAULT_HEADERS = {
    "User-Agent": "blender-mcp-fleet/1.0",
    "Accept": "application/json",
}

# Resolution key preference order (highest available within budget)
_RES_PREFERENCE = {
    "1k": ["1K-PNG", "1K-JPG"],
    "2k": ["2K-PNG", "2K-JPG", "1K-PNG", "1K-JPG"],
    "4k": ["4K-PNG", "4K-JPG", "2K-PNG", "2K-JPG", "1K-PNG", "1K-JPG"],
    "8k": ["8K-PNG", "8K-JPG", "4K-PNG", "4K-JPG", "2K-PNG", "2K-JPG"],
}

# Map of ambientCG texture suffixes to Blender node input names
TEXTURE_MAP: dict[str, dict[str, Any]] = {
    "Color":            {"input": "Base Color",        "colorspace": "sRGB",    "required": True},
    "NormalGL":         {"input": "Normal",            "colorspace": "Non-Color","required": False, "normal_map": True},
    "Roughness":        {"input": "Roughness",         "colorspace": "Non-Color","required": False},
    "Metalness":        {"input": "Metallic",          "colorspace": "Non-Color","required": False},
    "AmbientOcclusion": {"input": None,                "colorspace": "Non-Color","required": False, "ao": True},
    "Displacement":     {"input": "Displacement",      "colorspace": "Non-Color","required": False, "displacement": True},
}


class AmbientCGAPI:
    """Thin async client for the ambientCG JSON API."""

    def __init__(self, timeout: float = 60.0) -> None:
        self._timeout = timeout

    async def _get(self, params: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout, headers=DEFAULT_HEADERS) as client:
            response = await client.get(BASE_URL, params=params)
            response.raise_for_status()
            return response.json()

    async def search(
        self,
        query: str = "",
        limit: int = 20,
        sort: str = "Popular",
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search for PBR materials. Returns a slim list of asset dicts."""
        params: dict[str, Any] = {
            "include": "downloadData",
            "type": "Material",
            "limit": min(max(1, limit), 100),
            "offset": max(0, offset),
            "sort": sort,
        }
        if query.strip():
            params["q"] = query.strip()

        data = await self._get(params)
        assets = data.get("foundAssets") or []
        return [_slim_asset(a) for a in assets]

    async def asset_info(self, asset_id: str) -> dict[str, Any]:
        """Fetch full metadata for a single asset."""
        params = {
            "include": "downloadData",
            "type": "Material",
            "id": asset_id,
            "limit": 1,
        }
        data = await self._get(params)
        assets = data.get("foundAssets") or []
        if not assets:
            raise ValueError(f"ambientCG: asset '{asset_id}' not found")
        return assets[0]


def _slim_asset(asset: dict[str, Any]) -> dict[str, Any]:
    folders = asset.get("downloadFolders") or {}
    return {
        "id": asset.get("assetId", ""),
        "name": asset.get("displayName", ""),
        "resolutions": sorted(folders.keys()),
        "preview": asset.get("previewImage", ""),
    }


def resolve_download_url(asset: dict[str, Any], resolution: str) -> tuple[str, str]:
    """Return (download_url, resolution_key) for the best available resolution.

    Falls back down the preference list; raises ValueError if nothing matches.
    """
    folders: dict[str, Any] = asset.get("downloadFolders") or {}
    pref_key = resolution.lower().rstrip("k") + "k" if not resolution.lower().endswith("k") else resolution.lower()
    preferences = _RES_PREFERENCE.get(pref_key, _RES_PREFERENCE["2k"])

    for res_key in preferences:
        entry = folders.get(res_key)
        if entry and entry.get("downloadLink"):
            return str(entry["downloadLink"]), res_key

    # Last resort: any available
    for res_key, entry in folders.items():
        if entry and entry.get("downloadLink"):
            return str(entry["downloadLink"]), res_key

    raise ValueError(f"No download URL found for asset '{asset.get('assetId')}' at resolution '{resolution}'")


def build_pbr_material_script(mat_name: str, texture_files: dict[str, str]) -> str:
    """Generate a bpy script that creates a Principled BSDF material from texture paths.

    texture_files: dict mapping ambientCG suffix (e.g. 'Color', 'NormalGL') to absolute file path.
    """
    # Build the texture assignments as a Python literal so repr() keeps it injection-safe
    assignments = {suffix: path for suffix, path in texture_files.items() if suffix in TEXTURE_MAP}
    assignments_repr = repr(assignments)
    mat_name_repr = repr(mat_name)

    return f"""
import bpy, os

mat_name = {mat_name_repr}
assignments = {assignments_repr}

# Remove existing material with same name
if mat_name in bpy.data.materials:
    bpy.data.materials.remove(bpy.data.materials[mat_name])

mat = bpy.data.materials.new(name=mat_name)
mat.use_nodes = True
nt = mat.node_tree
nt.nodes.clear()

out = nt.nodes.new("ShaderNodeOutputMaterial")
out.location = (900, 0)
bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
bsdf.location = (500, 0)
nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

# Track AO mix node if needed
ao_tex = None

x = -400
for suffix, path in assignments.items():
    if not os.path.isfile(path):
        continue

    colorspace_map = {{
        "Color": "sRGB",
        "NormalGL": "Non-Color",
        "Roughness": "Non-Color",
        "Metalness": "Non-Color",
        "AmbientOcclusion": "Non-Color",
        "Displacement": "Non-Color",
    }}
    input_map = {{
        "Color": "Base Color",
        "Roughness": "Roughness",
        "Metalness": "Metallic",
    }}

    img = bpy.data.images.load(path)
    img.colorspace_settings.name = colorspace_map.get(suffix, "Non-Color")

    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.location = (x, 0)
    x -= 300

    if suffix in input_map:
        nt.links.new(tex.outputs["Color"], bsdf.inputs[input_map[suffix]])

    elif suffix == "NormalGL":
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nm.location = (tex.location[0] + 300, tex.location[1])
        nt.links.new(tex.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])

    elif suffix == "AmbientOcclusion":
        ao_tex = tex

    elif suffix == "Displacement":
        disp = nt.nodes.new("ShaderNodeDisplacement")
        disp.location = (600, -400)
        nt.links.new(tex.outputs["Color"], disp.inputs["Height"])
        nt.links.new(disp.outputs["Displacement"], out.inputs["Displacement"])

# Mix AO into Base Color if both present
if ao_tex is not None:
    for lnk in list(nt.links):
        if lnk.to_node == bsdf and lnk.to_socket.name == "Base Color":
            color_out = lnk.from_socket
            nt.links.remove(lnk)
            mix = nt.nodes.new("ShaderNodeMixRGB")
            mix.blend_type = "MULTIPLY"
            mix.inputs["Fac"].default_value = 1.0
            mix.location = (200, 200)
            nt.links.new(color_out, mix.inputs["Color1"])
            nt.links.new(ao_tex.outputs["Color"], mix.inputs["Color2"])
            nt.links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
            break

print("AMBIENTCG_MAT_OK:" + mat_name)
"""


__all__ = [
    "AmbientCGAPI",
    "TEXTURE_MAP",
    "build_pbr_material_script",
    "resolve_download_url",
]
