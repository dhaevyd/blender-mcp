"""Helpers injected into Blender scripts so they run on 4.x and 5.x.

Blender 5.0 moved keyframes from ``action.fcurves`` to layered actions
(``action.layers[].strips[].channelbag(slot).fcurves``). These snippets are plain
Python text, pasted into scripts that run inside Blender, so they must not import
anything from this package.
"""

# Defines _all_fcurves(animation_data) and _remove_fcurve(animation_data, fcurve).
ACTION_FCURVE_HELPERS = """
def _all_fcurves(animation_data):
    action = animation_data.action if animation_data else None
    if action is None:
        return []
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return list(legacy)
    curves = []
    for layer in action.layers:
        for strip in layer.strips:
            bag = strip.channelbag(animation_data.action_slot)
            if bag is not None:
                curves.extend(bag.fcurves)
    return curves

def _remove_fcurve(animation_data, fcurve):
    action = animation_data.action
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        legacy.remove(fcurve)
        return
    for layer in action.layers:
        for strip in layer.strips:
            bag = strip.channelbag(animation_data.action_slot)
            if bag is None:
                continue
            for existing in list(bag.fcurves):
                if existing == fcurve:
                    bag.fcurves.remove(existing)
                    return
"""

# Picks the EEVEE identifier this Blender build actually accepts (4.2-4.x: BLENDER_EEVEE_NEXT;
# 5.x: BLENDER_EEVEE). Checks the enum rather than comparing version numbers.
EEVEE_ENGINE_SELECT = (
    "_eevee_ids = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items]\n"
    "_eevee = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _eevee_ids else 'BLENDER_EEVEE'"
)
