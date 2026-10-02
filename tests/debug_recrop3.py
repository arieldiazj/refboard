"""Debug 3: skewed/trapezoid crop quads -> does hovering the visible
top/bottom edge return the matching zone?"""
import math as _m
import os
import sys
import tempfile
import types

import bpy


def fake_region(w=800, h=600):
    return types.SimpleNamespace(width=w, height=h, as_pointer=lambda: 1)


def make_png(path, w=100, h=100):
    img = bpy.data.images.new("t", width=w, height=h)
    img.pixels[:] = [0.9, 0.2, 0.2, 1.0] * (w * h)
    img.filepath_raw = path
    img.file_format = 'PNG'
    try:
        img.save()
    except Exception:
        img.save_render(path)
    bpy.data.images.remove(img)
    return path


def main():
    bpy.ops.preferences.addon_enable(module="refboard")
    import refboard as ct
    scene = bpy.context.scene
    reg = fake_region(800, 600)
    tmp = tempfile.mkdtemp(prefix="rb_dbg_")
    png = make_png(os.path.join(tmp, "a.png"))

    cases = [
        ("trapezoid: top edge tilted", 0.0,
         [(0.2, 0.2), (0.8, 0.2), (1.0, 0.9), (0.4, 0.7)]),
        ("skewed quad rot30", 30.0,
         [(0.1, 0.1), (0.9, 0.3), (0.8, 0.9), (0.05, 0.6)]),
        ("heavily skewed quad rot30", 30.0,
         [(0.05, 0.4), (0.95, 0.1), (0.9, 0.6), (0.3, 0.95)]),
        ("concave-ish quad", 0.0,
         [(0.2, 0.2), (0.8, 0.2), (0.7, 1.0), (0.3, 0.5)]),
    ]
    for name, rot_deg, pts in cases:
        scene.refboard_items.clear()
        it = scene.refboard_items.add()
        it.image = bpy.data.images.load(png)
        it.pos = (0.5, 0.5)
        it.scale = (1.0, 1.0)
        it.rotation = _m.radians(rot_deg)
        ct._refboard_crop_commit(it, pts)
        scene.refboard_selected = 0
        print(f"\n=== {name} ===")
        q = ct._refboard_crop_quad(it, reg, it.image)
        print("quad:", [(round(x, 1), round(y, 1)) for x, y in q])
        rect = ct._refboard_rect(it, reg, it.image,
                                 ct._refboard_view(scene, reg))
        print("rect rot deg:", round(_m.degrees(rect[6]), 1),
              "hx,hy:", round(rect[2], 1), round(rect[3], 1))
        for nm, (a, b) in {"left(3-0)": (3, 0), "bot(0-1)": (0, 1),
                           "right(1-2)": (1, 2), "top(2-3)": (2, 3)}.items():
            mx, my = (q[a][0] + q[b][0]) * 0.5, (q[a][1] + q[b][1]) * 0.5
            z = ct._refboard_zone(rect, mx, my, True, True)
            print(f"  quad edge {nm} mid=({mx:.1f},{my:.1f}) -> zone {z}")
    return 0


sys.exit(main())
