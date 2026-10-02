"""Debug 2: rotated image + axis-aligned UV crop -> which edge does the
screen-top hover pick?"""
import math as _m
import os
import sys
import tempfile
import types

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))


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

    for rot_deg in (30.0, 60.0, 120.0, 200.0, 330.0):
        scene.refboard_items.clear()
        it = scene.refboard_items.add()
        it.image = bpy.data.images.load(png)
        it.pos = (0.5, 0.5)
        it.scale = (1.0, 1.0)
        it.rotation = _m.radians(rot_deg)
        it.crop = (0.2, 0.2, 0.8, 0.8)   # axis-aligned UV crop
        scene.refboard_selected = 0
        print(f"\n=== rot {rot_deg}, UV rect crop ===")
        q = ct._refboard_crop_quad(it, reg, it.image)
        print("quad:", [(round(x, 1), round(y, 1)) for x, y in q])
        rect = ct._refboard_rect(it, reg, it.image,
                                 ct._refboard_view(scene, reg))
        print("rect rot deg:", round(_m.degrees(rect[6]), 1),
              "hx,hy:", round(rect[2], 1), round(rect[3], 1))
        # midpoints of each stored quad edge
        pairs = {"left(3-0)": (3, 0), "bot(0-1)": (0, 1),
                 "right(1-2)": (1, 2), "top(2-3)": (2, 3)}
        for name, (a, b) in pairs.items():
            mx, my = (q[a][0] + q[b][0]) * 0.5, (q[a][1] + q[b][1]) * 0.5
            z = ct._refboard_zone(rect, mx, my, True, True)
            print(f"  quad edge {name} mid=({mx:.1f},{my:.1f})"
                  f" -> zone {z}")

    # Case B: marquee-cropped rotated image, then ROTATED again by the user
    for rot_deg in (60.0, 150.0, 240.0):
        scene.refboard_items.clear()
        it = scene.refboard_items.add()
        it.image = bpy.data.images.load(png)
        it.pos = (0.5, 0.5)
        it.scale = (1.0, 1.0)
        it.rotation = 0.0
        it.crop = (0.0, 0.0, 1.0, 1.0)
        scene.refboard_selected = 0
        ct._refboard_apply_crop_rect(scene, reg, 0, 370, 290, 430, 310)
        it.rotation = _m.radians(rot_deg)   # user rotates the cropped ref
        print(f"\n=== marquee crop then rotate {rot_deg} ===")
        q = ct._refboard_crop_quad(it, reg, it.image)
        print("quad:", [(round(x, 1), round(y, 1)) for x, y in q])
        rect = ct._refboard_rect(it, reg, it.image,
                                 ct._refboard_view(scene, reg))
        print("rect rot deg:", round(_m.degrees(rect[6]), 1))
        for name, (a, b) in {"left(3-0)": (3, 0), "bot(0-1)": (0, 1),
                             "right(1-2)": (1, 2), "top(2-3)": (2, 3)}.items():
            mx, my = (q[a][0] + q[b][0]) * 0.5, (q[a][1] + q[b][1]) * 0.5
            z = ct._refboard_zone(rect, mx, my, True, True)
            print(f"  quad edge {name} mid=({mx:.1f},{my:.1f})"
                  f" -> zone {z}")
    return 0


sys.exit(main())
