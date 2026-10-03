import bpy
import hashlib
import math
import os
import platform
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
import ctypes
import uuid
from mathutils import Vector, Matrix
from bpy_extras import view3d_utils
from bpy.app.handlers import persistent

try:
    import gpu
    import blf
    from gpu_extras.batch import batch_for_shader
except Exception:
    gpu = None
    blf = None
    batch_for_shader = None

bl_info = {
    "name": "Refboard",
    "author": "AD",
    "version": (0, 2, 1),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Refboard",
    "category": "3D View",
    "description": "Reference image board drawn over the 3D viewport",
    "support": "COMMUNITY",
}

ADDON_NAME = __name__


def _get_prefs(context):
    return context.preferences.addons[ADDON_NAME].preferences


def _refboard_pref(name, default):
    """Addon-preference read that survives contexts where the addon entry is
    unavailable (draw handlers in odd contexts, tests before registration)."""
    try:
        return getattr(_get_prefs(bpy.context), name)
    except Exception:
        return default


class RefboardPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_NAME

    show_n_panel: bpy.props.BoolProperty(
        name="Show N Panel",
        default=False,
        description="Keep the 3D view sidebar open while editing the "
                    "Refboard. When off, entering edit mode hides the "
                    "N-panel and restores it on exit.",
    )

    show_help: bpy.props.BoolProperty(
        name="Show Help", default=True,
        update=lambda self, context: _refboard_redraw_views())

    help_size: bpy.props.FloatProperty(
        name="Help Text Size", default=16.0, min=6.0, max=36.0,
        update=lambda self, context: _refboard_help_size_update(self, context))

    veil_color: bpy.props.FloatVectorProperty(
        name="Veil Color", size=3, subtype='COLOR',
        default=(0.286, 0.282, 0.353), min=0.0, max=1.0,
        update=lambda self, context: _refboard_veil_update(self, context))

    veil_alpha: bpy.props.FloatProperty(
        name="Veil Opacity", default=0.7, min=0.0, max=1.0,
        update=lambda self, context: _refboard_veil_update(self, context))

    def draw(self, context):
        col = self.layout.column(align=True)
        col.prop(self, "show_n_panel")
        col.prop(self, "show_help")
        sub = col.column(align=True)
        sub.enabled = self.show_help
        sub.prop(self, "help_size")
        row = col.row(align=True)
        row.prop(self, "veil_color", text="Veil")
        row.prop(self, "veil_alpha", text="", slider=True)
        row = col.row(align=True)
        row.operator("refboard.update_check", icon='FILE_REFRESH')
        if _refboard_update.get("msg"):
            col.label(text=_refboard_update["msg"])


# --- image repository ---------------------------------------------------------
#
# Pasted images live in a "refboard" folder next to the .blend instead of being
# packed into it, so the scene stays light. Files are content-addressed: the name
# is a hash of the bytes, so the same image pasted into twenty incremental saves
# is stored exactly once, and every .blend in that folder shares it through the
# relative path //refboard/<hash>.<ext>.
#
# Repo files are never deleted automatically: a file that looks unused from
# scene_v047.blend may still be referenced by scene_v012.blend sitting beside it.

_REFBOARD_REPO_NAME = "refboard"


def _refboard_repo_dir(blend_path=None, create=False):
    """The repo folder next to the .blend, or None while the file is unsaved."""
    bp = blend_path if blend_path is not None else bpy.data.filepath
    if not bp:
        return None
    d = os.path.join(os.path.dirname(os.path.abspath(bp)), _REFBOARD_REPO_NAME)
    if create:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            print("Refboard: could not create repo folder:", e)
            return None
    return d


def _refboard_cache_dir(create=True):
    """Holding area for pastes made before the .blend has ever been saved."""
    d = os.path.join(tempfile.gettempdir(), "refboard_unsaved")
    if create:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return tempfile.gettempdir()
    return d


def _refboard_hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _refboard_store_file(src, blend_path=None):
    """Copy src into the repo under a content-addressed name.

    Returns the absolute stored path, or src itself if nothing could be written.
    """
    if not src or not os.path.isfile(src):
        return src
    d = _refboard_repo_dir(blend_path, create=True) or _refboard_cache_dir()
    ext = os.path.splitext(src)[1].lower() or ".png"
    try:
        dst = os.path.join(d, _refboard_hash_file(src) + ext)
        if os.path.abspath(dst) == os.path.abspath(src):
            return dst
        # Already in the repo from an earlier paste or an earlier .blend version.
        if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
            shutil.copyfile(src, dst)
        return dst
    except Exception as e:
        print("Refboard: could not store image in repo:", e)
        return src


def _refboard_in_repo(img, blend_path=None):
    """True if the image already points inside the repo for this .blend."""
    repo = _refboard_repo_dir(blend_path)
    if not repo or not img or img.packed_file:
        return False
    try:
        p = bpy.path.abspath(img.filepath_raw)
    except Exception:
        return False
    if not p:
        return False
    return os.path.normcase(os.path.dirname(os.path.abspath(p))) == \
        os.path.normcase(os.path.abspath(repo))


def _refboard_relink(img, path):
    """Point an image at a repo file, relative to the .blend when possible."""
    try:
        img.filepath_raw = path
        if bpy.data.filepath:
            try:
                img.filepath_raw = bpy.path.relpath(path)
            except Exception:
                pass
        img.use_fake_user = False
        img.reload()
    except Exception as e:
        print("Refboard: could not relink image:", e)


def _refboard_adopt(src):
    """Store a freshly pasted file in the repo and return its image datablock.

    check_existing means re-pasting the same picture reuses one datablock rather
    than stacking up Refboard.001, Refboard.002 ...
    """
    stored = _refboard_store_file(src)
    img = bpy.data.images.load(stored, check_existing=True)
    img.use_fake_user = False
    if bpy.data.filepath:
        try:
            img.filepath_raw = bpy.path.relpath(stored)
        except Exception:
            pass
    return img


def _refboard_tracked_images():
    """Images the addon is responsible for: board items, plus our own empties."""
    imgs = []
    seen = set()

    def add(img):
        if img is None:
            return
        key = img.as_pointer()
        if key not in seen:
            seen.add(key)
            imgs.append(img)

    for sc in bpy.data.scenes:
        for it in getattr(sc, "refboard_items", []) or []:
            add(it.image)
    for ob in bpy.data.objects:
        if ob.type == 'EMPTY' and ob.name.startswith("Refboard"):
            data = getattr(ob, "data", None)
            if isinstance(data, bpy.types.Image):
                add(data)
    return imgs


def _refboard_unpack(img):
    """Drop packed bytes from an image, leaving it referencing its filepath."""
    for method in ('REMOVE', 'USE_ORIGINAL'):
        try:
            img.unpack(method=method)
            return True
        except Exception:
            continue
    return False


def _refboard_externalize(blend_path=None):
    """Move every tracked image out of the .blend and into the repo.

    Runs on save, which is also what slims down scenes that were created by an
    older version of this addon with images packed inside them.
    """
    repo = _refboard_repo_dir(blend_path, create=True)
    if not repo:
        return 0
    moved = 0
    for img in _refboard_tracked_images():
        try:
            if _refboard_in_repo(img, blend_path):
                continue
            src = None
            if img.packed_file:
                # Prefer the exact packed bytes so nothing is re-encoded.
                data = getattr(img.packed_file, "data", None)
                ext = os.path.splitext(img.filepath_raw or "")[1].lower()
                if not ext:
                    ext = ".png"
                tmp = os.path.join(_refboard_cache_dir(),
                                   "unpack_%s%s" % (uuid.uuid4().hex, ext))
                if data:
                    with open(tmp, "wb") as f:
                        f.write(data)
                    src = tmp
                else:
                    try:
                        img.save(filepath=tmp, save_copy=True)
                        src = tmp
                    except Exception:
                        src = None
            else:
                p = bpy.path.abspath(img.filepath_raw)
                src = p if p and os.path.isfile(p) else None

            if not src:
                continue
            stored = _refboard_store_file(src, blend_path)
            if img.packed_file:
                img.filepath_raw = stored
                _refboard_unpack(img)
            _refboard_relink(img, stored)
            moved += 1
            if src.startswith(_refboard_cache_dir(create=False)):
                try:
                    os.remove(src)
                except Exception:
                    pass
        except Exception as e:
            print("Refboard: could not externalize image:", e)
    return moved


def _refboard_drop_image(img):
    """Unload an image datablock once nothing references it any more."""
    if img is None:
        return
    try:
        img.use_fake_user = False
        if img.users == 0:
            bpy.data.images.remove(img)
    except Exception:
        pass


def _refboard_gc_images(images=None):
    """Unload orphaned Refboard images so deleted refs stop weighing on the file.

    Only touches images that live in our repo or cache, so a user's own unused
    images are never silently removed.
    """
    if images is None:
        images = [i for i in bpy.data.images
                  if _refboard_in_repo(i) or
                  (i.filepath_raw or "").find(_REFBOARD_REPO_NAME) != -1]
    for img in list(images):
        _refboard_drop_image(img)


# ---------------------------------------------------------------------------
# Refboard — clipboard image paste, as a 3D empty or a screen-space overlay.
#
# Screen-space images are drawn by a POST_PIXEL draw handler scoped to
# SpaceView3D, so they never appear over the UV editor, node editor, or any
# other space, and they ignore the camera entirely. Placement is stored as a
# normalized fraction of the viewport so it survives window resizes and file
# reloads. All interaction (select / drag / scale / rotate / crop / delete)
# runs through a single persistent modal operator that only swallows events it
# needs, so navigation (MMB, Alt) always passes through.
# ---------------------------------------------------------------------------

_REFBOARD_HANDLER = None
_REFBOARD_TEX_SHADER = None
_REFBOARD_TEX_SHADER_NEEDS_MVP = False
_REFBOARD_FLAT_SHADER = None
_REFBOARD_FLAT_SHADER_NEEDS_MVP = False
_REFBOARD_SMOOTH_SHADER = None
_REFBOARD_SMOOTH_SHADER_NEEDS_MVP = False

# In-flight clipboard reads: dicts with proc, dst, mode, and the view state
# captured when Ctrl+V was pressed.
_refboard_pending = []

# Active drag state while the interact modal has a grab in progress.
_refboard_drag = None

# (region pointer, corner index) while the mouse sits in a rotate zone or a
# rotate drag is live - the draw handler wraps an arc around that corner.
_refboard_hover = None

# (region pointer, item index, hide_after): shows the opacity % readout
# while an opacity drag is live and for one second after release.
_refboard_opacity_label = None

# Active canvas pan (MMB or Alt+MMB drag) state: {"pan0", "m0"}.
_refboard_pan_drag = None

# Active canvas zoom (Alt+RMB drag) state: {"zoom0", "pan0", "m0"}.
_refboard_zoom_drag = None

# Last time the veil color/opacity was touched; keeps the veil previewed
# at full strength while the prefs are dragged, then fades out over 1.5s.
_refboard_veil_ts = 0.0

# Marquee group selection: indices into scene.refboard_items. When non-empty it
# replaces the single-ref selection as the active edit target.
_refboard_group = []

# Live marquee drag state: {"ptr", "m0", "cur", "scene"}.
_refboard_marquee = None
# Live crop-marquee state (Ctrl+LMB drag on an image):
# {"ptr", "region", "idx", "m0", "cur"}.
_refboard_cropmarq = None

# Live flick state (Ctrl+Shift+LMB press on an image):
# {"ptr", "idx", "m0", "t0", "done"}. A fast dominant-axis drag flips the
# image once, then the rest of the gesture is swallowed until release.
_refboard_flick = None
_REFBOARD_FLICK_DIST = 28.0   # px of travel needed to fire the flip
_REFBOARD_FLICK_TIME = 0.5    # press->crossing window; a slow drag is inert

# Manual double-click tracking for crop restore: (timestamp, x, y, idx)
# of the last Ctrl+press that landed on an image. A second press on the
# same ref inside the threshold restores the full crop - more reliable
# than event.value=='DOUBLE_CLICK', which can get swallowed by the first
# press's edge-drag.
_refboard_cropedge_click = None

# Stashed context for the canvas-mode RMB menu (e.g. the region the press
# happened over, since menu ops get a different context region).
_refboard_ctx = {}

# Canvas mode: while on, Refboard images are interactive (move/scale/
# rotate/crop/opacity/marquee) and ALL viewport input is owned so the 3D
# scene can't be touched. Alt+` enters it; ` or Alt+` leaves it, as does Esc.
_refboard_canvas_on = False

# Board-local undo, kept fully separate from Blender's memfile queue:
# Refboard edits push snapshots of board state onto `past`, and Ctrl+Z /
# Ctrl+Shift+Z inside edit mode step through them - so Blender's undo
# never sees a Refboard step and Refboard undo only exists in edit mode.
# `prev` is the last committed resting state, seeded when edit mode opens
# or the board changes; each undo_push stores the gesture's PRE state
# (every action's pre-state is the previous action's post-state).
_refboard_undo = {"scene": None, "prev": None, "past": [], "future": []}

# SpaceView3D objects whose UI region (N-panel) we collapsed when edit mode
# was entered. Only spaces that had it visible are listed, so exit restores
# exactly what it took and leaves previously-hidden ones alone.
_refboard_npanel_hidden = []


def _refboard_view3d_spaces():
    try:
        for w in bpy.context.window_manager.windows:
            if w.screen is None:
                continue
            for a in w.screen.areas:
                if a.type == 'VIEW_3D':
                    yield a.spaces.active
    except Exception:
        return


def _refboard_npanel_hide(spaces=None):
    """Collapse the N-panel in every 3D view that has it open, remembering
    which ones were ours to hide. No-op when 'Show N Panel' is on."""
    try:
        if _get_prefs(bpy.context).show_n_panel:
            return
    except Exception:
        pass
    if spaces is None:
        spaces = _refboard_view3d_spaces()
    for sp in spaces:
        try:
            if sp.show_region_ui:
                sp.show_region_ui = False
                _refboard_npanel_hidden.append(sp)
        except Exception:
            pass


def _refboard_npanel_restore():
    """Put back the N-panel on spaces we hid it from; a panel that was closed
    before edit mode was never recorded and so stays closed."""
    for sp in _refboard_npanel_hidden:
        try:
            sp.show_region_ui = True
        except Exception:
            pass
    del _refboard_npanel_hidden[:]

# Last time an overlay was selected; the help block fades out over 1.5s
# after the selection clears.
_refboard_help_ts = 0.0

# Mode-label flash ("Edit" / "Exit" / "Off") shown bottom-center for
# 1.5s after each mode switch.
_refboard_mode_ts = 0.0
_refboard_mode_label = ""

# Whether Ctrl is held - in canvas mode this hides the scale markers and
# turns the border edges into crop zones.
_refboard_mod_ctrl = False

_refboard_modal_running = False

# View state captured at Ctrl+V time so the popup menu choice can paste where
# the mouse actually was.
_refboard_menu_state = {}

_refboard_keymaps = []

_REFBOARD_HANDLE_R = 5.0      # drawn edge-dot radius
_REFBOARD_CORNER_R = 6.0      # drawn corner-dot radius (bigger, per feedback)
_REFBOARD_HANDLE_HIT = 8.0    # clickable radius around a dot
_REFBOARD_ROT_IN = 24.0       # rotate zone starts this far past a corner
_REFBOARD_ROT_OUT = 48.0      # and ends this far out
_REFBOARD_ROT_R = 30.0        # drawn arc radius, inside the zone band
_REFBOARD_BORDER_HIT = 5.0    # clickable half-width of the border (crop)


class RefboardItem(bpy.types.PropertyGroup):
    """One screen-space image. Position is a normalized viewport fraction so a
    saved file restores the same layout at any window size."""
    image: bpy.props.PointerProperty(
        type=bpy.types.Image,
        name="Image",
    )
    pos: bpy.props.FloatVectorProperty(
        name="Position",
        size=2,
        default=(0.5, 0.5),
    )
    scale: bpy.props.FloatVectorProperty(
        name="Scale",
        size=2,
        default=(1.0, 1.0),
        min=0.001,
    )
    rotation: bpy.props.FloatProperty(
        name="Rotation",
        default=0.0,
    )
    # Crop as a texture-space rect (left, bottom, right, top): the AABB of
    # crop_pts, kept for bookkeeping. Writing it directly re-syncs crop_pts
    # to the plain axis-aligned rect via the update callback.
    crop: bpy.props.FloatVectorProperty(
        name="Crop",
        size=4,
        default=(0.0, 0.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
        update=lambda self, context: _refboard_crop_rect_update(self),
    )
    # Visible region as an arbitrary UV quad (BL,BR,TR,TL corner order). A
    # screen-space marquee crop on a rotated or non-uniformly scaled image
    # maps to a parallelogram in UV space that a plain rect cannot express;
    # storing the four corners renders it exactly. `crop` remains the
    # quad's axis-aligned bounds for bookkeeping; the two are kept in sync
    # by _refboard_crop_commit().
    crop_pts: bpy.props.FloatVectorProperty(
        name="Crop Quad",
        size=8,
        default=(0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0),
        min=-4.0,
        max=4.0,
    )
    # Display-only fade. Floored at 1% so an image can never be faded out of
    # existence by accident.
    opacity: bpy.props.FloatProperty(
        name="Opacity",
        default=1.0,
        min=0.01,
        max=1.0,
        update=lambda self, context: _refboard_redraw_views(),
    )
    visible: bpy.props.BoolProperty(
        name="Visible",
        default=True,
        update=lambda self, context: _refboard_flag_update(self, context),
    )
    # Locked refs still draw but can't be clicked or dragged - strokes pass
    # straight through to grease pencil / sculpt.
    locked: bpy.props.BoolProperty(
        name="Locked",
        default=False,
        update=lambda self, context: _refboard_flag_update(self, context),
    )
    # Mirror flips: texcoord-only. Quad positions, hit zones and the crop
    # frame are all left untouched, so flips compose freely with crops and
    # rotations. scale stays positive-only (min=0.001) so a flip can't be
    # folded into it.
    flip_x: bpy.props.BoolProperty(
        name="Flip Horizontal",
        default=False,
        options={'HIDDEN'},
        update=lambda self, context: _refboard_redraw_views(),
    )
    flip_y: bpy.props.BoolProperty(
        name="Flip Vertical",
        default=False,
        options={'HIDDEN'},
        update=lambda self, context: _refboard_redraw_views(),
    )
    # The last user-chosen scale. Neighbor yields floor at MIN times this,
    # never at the gesture's (possibly already-shrunk) starting scale - so
    # back-to-back crowding can never crush a ref below one bounded size.
    # (0,0) = unset, filled lazily at the first gesture.
    home_scale: bpy.props.FloatVectorProperty(
        name="Home Scale",
        size=2,
        default=(0.0, 0.0),
        options={'HIDDEN'},
    )


# --- clipboard plumbing (ported from ad_quick_paste) -------------------------

def _refboard_clipboard_formats():
    """Formats physically present on the clipboard, via EnumClipboardFormats.

    IsClipboardFormatAvailable *synthesizes* - a lone CF_BITMAP (an app
    thumbnail, a file icon, .NET's SetImage) reports CF_DIB available and
    GetClipboardData converts it into a fake image. Only formats in this
    enumeration are real."""
    fmts = set()
    fmt = 0
    while True:
        fmt = _refboard_u32.EnumClipboardFormats(fmt)
        if not fmt:
            break
        fmts.add(fmt)
    return fmts


def _refboard_clipboard_is_files(fmts):
    """True when the clipboard describes files/OLE objects rather than an
    image: the bitmap riding along in those is the item's icon."""
    if 15 in fmts:                                   # CF_HDROP
        return True
    for name in ("FileGroupDescriptor", "FileGroupDescriptorW",
                 "FileContents", "Object Descriptor"):
        fmt = _refboard_u32.RegisterClipboardFormatW(name)
        if fmt and fmt in fmts:
            return True
    return False


def _refboard_clipboard_has_image():
    """True when the OS clipboard can currently serve an image.

    Replaces the old sequence-number freshness gate: what gates Ctrl+V is
    *content*, not age - an image copied before Blender even started is
    just as pastable. A clipboard holding anything else (text, files,
    Blender-internal copies) returns False so the event falls through to
    Blender's own paste.
    """
    if platform.system() != "Windows" or _refboard_u32 is None:
        return True
    for _ in range(10):
        if _refboard_u32.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        return False
    try:
        for name in ("PNG", "image/png"):
            fmt = _refboard_u32.RegisterClipboardFormatW(name)
            if fmt and _refboard_u32.IsClipboardFormatAvailable(fmt):
                return True
        fmts = _refboard_clipboard_formats()
        if _refboard_clipboard_is_files(fmts):
            return False
        # A real DIBV5/DIB on the clipboard, or a bare CF_BITMAP: the lone
        # bitmap can be a genuine image (WinForms copies) or an icon - the
        # decoded-size floor in _refboard_finish sorts that out.
        return bool(fmts & {17, 8, 2})
    finally:
        _refboard_u32.CloseClipboard()


def _refboard_download_url(url):
    """Browser image drags deliver a URL, not a file. Fetch to a temp file so
    the normal load/pack path handles it. Returns the temp path."""
    import urllib.request
    base = url.split("?")[0].split("#")[0]
    ext = os.path.splitext(base)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff",
                   ".webp", ".gif", ".exr", ".hdr"):
        ext = ".png"
    dst = os.path.join(
        tempfile.gettempdir(), "refboard_%s%s" % (uuid.uuid4().hex, ext))
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    if not data:
        raise RuntimeError("empty response")
    with open(dst, "wb") as f:
        f.write(data)
    return dst


_refboard_u32 = None
_refboard_k32 = None
if platform.system() == "Windows":
    try:
        _refboard_u32 = ctypes.windll.user32
        _refboard_k32 = ctypes.windll.kernel32
        # Handles/pointers must be declared or ctypes truncates them to 32
        # bits on Win64 and the reads come back garbage.
        _refboard_u32.GetClipboardData.restype = ctypes.c_void_p
        _refboard_u32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
        _refboard_k32.GlobalLock.argtypes = [ctypes.c_void_p]
        _refboard_k32.GlobalLock.restype = ctypes.c_void_p
        _refboard_k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        _refboard_k32.GlobalSize.argtypes = [ctypes.c_void_p]
        _refboard_k32.GlobalSize.restype = ctypes.c_size_t
    except Exception:
        _refboard_u32 = _refboard_k32 = None


def _refboard_dib_to_bmp(raw):
    """Convert raw DIB bytes (BITMAPINFOHEADER + palette + pixels) into a
    file Blender can load: a BMP wrapper for plain bitmaps, or the embedded
    stream itself for BI_JPEG/BI_PNG DIBs. Returns (ext, bytes) or None for
    payloads the wrapper can't honor (RLE, bad headers) - the caller falls
    back to the PowerShell grab for those."""
    if len(raw) < 36:
        return None
    hdr = struct.unpack_from("<I", raw, 0)[0]
    w = struct.unpack_from("<i", raw, 4)[0]
    h = struct.unpack_from("<i", raw, 8)[0]
    bpp = struct.unpack_from("<H", raw, 14)[0]
    comp = struct.unpack_from("<I", raw, 16)[0]
    used = struct.unpack_from("<I", raw, 32)[0]
    if hdr < 40 or hdr + 14 > len(raw) or w == 0 or h == 0 or             abs(w) > 16384 or abs(h) > 16384:
        return None
    ncolors = used or (1 << bpp if bpp <= 8 else 0)
    # On a 40-byte BITMAPINFOHEADER the bitfield masks live right after the
    # header (3 for BI_BITFIELDS, 4 for BI_ALPHABITFIELDS); V4/V5 headers
    # embed them already.
    masks = 0 if hdr > 40 else {3: 12, 6: 16}.get(comp, 0)
    off = 14 + hdr + ncolors * 4 + masks
    if off > 14 + len(raw):
        return None
    if comp in (4, 5):
        # BI_JPEG / BI_PNG: the "pixel" area IS a complete image file.
        ext = ".jpg" if comp == 4 else ".png"
        return ext, raw[off - 14:]
    if comp not in (0, 3, 6):
        return None
    return ".bmp", struct.pack(
        "<2sIHHI", b"BM", 14 + len(raw), 0, 0, off) + raw


def _refboard_clip_read_fmt(fmt):
    """Copy the clipboard payload for `fmt` out of its global handle."""
    h = _refboard_u32.GetClipboardData(fmt)
    if not h:
        return None
    size = _refboard_k32.GlobalSize(h)
    ptr = _refboard_k32.GlobalLock(h)
    if not ptr:
        return None
    try:
        return ctypes.string_at(ptr, size)
    finally:
        _refboard_k32.GlobalUnlock(h)


def _refboard_clipboard_image_win(dst_base):
    """Read the OS clipboard image in-process: no subprocess, ~ms not ~s.

    Prefers the registered PNG clipboard format (browsers/screenshot tools
    put real PNG bytes up, alpha intact), then CF_DIBV5/CF_DIB wrapped into
    a BMP file. Returns the written file's path, or None when the clipboard
    holds no usable image - the caller falls back to PowerShell for that.
    """
    if _refboard_u32 is None:
        return None
    for _ in range(10):
        if _refboard_u32.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        return None
    try:
        for name in ("PNG", "image/png"):
            fmt = _refboard_u32.RegisterClipboardFormatW(name)
            raw = _refboard_clip_read_fmt(fmt) if fmt else None
            # Some apps register a "PNG" format whose payload is anything
            # but - verify the signature or Blender loads garbage/magenta.
            if raw and raw[:8] == b"\x89PNG\r\n\x1a\n":
                out = os.path.splitext(dst_base)[0] + ".png"
                with open(out, "wb") as f:
                    f.write(raw)
                return out
        # A file/OLE clipboard's bitmap is the item's icon - skip it.
        if not _refboard_clipboard_is_files(_refboard_clipboard_formats()):
            for fmt in (17, 8):          # CF_DIBV5, CF_DIB
                raw = _refboard_clip_read_fmt(fmt)
                if raw:
                    conv = _refboard_dib_to_bmp(raw)
                    if conv:
                        ext, data = conv
                        out = os.path.splitext(dst_base)[0] + ext
                        with open(out, "wb") as f:
                            f.write(data)
                        return out
    finally:
        _refboard_u32.CloseClipboard()
    return None


def _refboard_paste_proc(dst_path):
    """Launch the PowerShell clipboard grab without blocking the UI."""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$img=[Windows.Forms.Clipboard]::GetImage();"
        "if ($img -eq $null) { exit 2 };"
        "$img.Save('" + dst_path.replace("'", "''") + "',"
        " [System.Drawing.Imaging.ImageFormat]::Png);"
    )
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )


def _refboard_copy_proc(src_path):
    """Push an image file onto the OS clipboard (async, STA powershell)."""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "[Windows.Forms.Clipboard]::SetImage("
        "[System.Drawing.Bitmap]::FromFile('" +
        src_path.replace("'", "''") + "'))"
    )
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        ["powershell", "-STA", "-NoProfile", "-NonInteractive",
         "-Command", ps],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )


def _refboard_copy_selected(scene):
    """Ctrl+C in canvas mode: the selected ref's image goes to the OS
    clipboard, so it can paste into other apps - or back onto the board."""
    idx = scene.refboard_selected
    if not (0 <= idx < len(scene.refboard_items)):
        return False
    img = scene.refboard_items[idx].image
    if img is None:
        return False
    path = bpy.path.abspath(img.filepath_raw) if img.filepath_raw else ""
    if not path or not os.path.isfile(path):
        # No backing file (packed or in-memory): render out a temp PNG.
        path = os.path.join(tempfile.gettempdir(),
                            "refboard_copy_%s.png" % uuid.uuid4().hex)
        try:
            img.save_render(path)
        except Exception:
            return False
    try:
        _refboard_copy_proc(path)
    except Exception:
        return False
    return True


# --- 3D placement helpers (ported from ad_quick_paste) ------------------------

def _refboard_view_basis(context):
    area = context.area
    space = context.space_data
    if (
        area and area.type == "VIEW_3D" and space
        and getattr(space, "region_3d", None) is not None
        and space.region_3d.view_perspective == "CAMERA"
        and context.scene.camera is not None
    ):
        m = context.scene.camera.matrix_world.to_3x3()
        return ((m @ Vector((0, 0, -1))).normalized(),
                (m @ Vector((1, 0, 0))).normalized(),
                (m @ Vector((0, 1, 0))).normalized())
    if area and area.type == "VIEW_3D" and space and \
            getattr(space, "region_3d", None) is not None:
        q = space.region_3d.view_rotation
        return ((q @ Vector((0, 0, -1))).normalized(),
                (q @ Vector((1, 0, 0))).normalized(),
                (q @ Vector((0, 1, 0))).normalized())
    return (Vector((0, 0, -1)), Vector((1, 0, 0)), Vector((0, 1, 0)))


def _refboard_cardinal_normal(view_dir):
    ax, ay, az = abs(view_dir.x), abs(view_dir.y), abs(view_dir.z)
    if ax >= ay and ax >= az:
        return Vector((1.0 if view_dir.x >= 0 else -1.0, 0, 0))
    if ay >= ax and ay >= az:
        return Vector((0, 1.0 if view_dir.y >= 0 else -1.0, 0))
    return Vector((0, 0, 1.0 if view_dir.z >= 0 else -1.0))


def _refboard_align_cardinal(obj, view_dir, normal, view_right, view_up):
    n = normal.normalized()
    vd = view_dir.normalized()
    if n.dot(vd) > 0.0:
        n.negate()
    r = view_right - n * view_right.dot(n)
    if r.length < 1e-6:
        r = view_up - n * view_up.dot(n)
    if r.length < 1e-6:
        r = n.orthogonal()
    r.normalize()
    if r.dot(view_right) < 0.0:
        r.negate()
    u = n.cross(r)
    if u.length < 1e-6:
        u = view_up - n * view_up.dot(n)
    if u.length < 1e-6:
        u = r.orthogonal()
    u.normalize()
    if u.dot(view_up) < 0.0:
        u.negate()
    r = u.cross(n)
    r.normalize()
    m = Matrix((r, u, n)).transposed().to_4x4()
    m.translation = obj.matrix_world.translation
    obj.matrix_world = m


# --- screen-space geometry ----------------------------------------------------

_REFBOARD_VIEW_IDENT = (1.0, (0.0, 0.0))


class _RefboardRegion:
    """Minimal stand-in for a Region, for math that needs a size but no area."""
    __slots__ = ("width", "height")

    def __init__(self, width, height):
        self.width = width
        self.height = height


def _refboard_ref_size(scene, region):
    """Viewport size the board's layout was authored at (the canvas size).

    Item positions are fractions of *this* size rather than of the live
    viewport. Unset (a board from before this existed) falls back to the live
    region, which reproduces the old behavior until the modal records a
    reference - see _refboard_ensure_ref_size.
    """
    rs = getattr(scene, "refboard_ref_size", None)
    # 64px is far below any plausible authoring viewport, so a smaller pin
    # can only have come from a transient mid-resize size - ignore it rather
    # than trusting it.
    w = float(rs[0]) if rs is not None and rs[0] >= 64.0 \
        else float(region.width)
    h = float(rs[1]) if rs is not None and rs[1] >= 64.0 \
        else float(region.height)
    return max(1.0, w), max(1.0, h)


def _refboard_fit(scene, region):
    """Uniform canvas px -> screen px factor.

    Driven by height alone and applied to positions *and* sizes, so the board
    is a similarity transform of the canvas: relative spacing is preserved.
    A width-only resize leaves this at 1.0 and simply re-centers the board.
    """
    _w0, h0 = _refboard_ref_size(scene, region)
    # Clamp the fit factor: a degenerate canvas pin or a transient resize
    # size must never explode the layout across the whole screen.
    return min(8.0, max(0.125, float(region.height) / h0))


def _refboard_view(scene, region=None):
    """Canvas view transform: (zoom, pan, canvas_w, canvas_h).

    zoom already carries the canvas fit factor, and pan already carries the
    centering offset, so every consumer scales offsets and sizes by the same
    amount without knowing the viewport changed.
    """
    zoom_u = getattr(scene, "refboard_view_zoom", 1.0)
    pan_u = getattr(scene, "refboard_view_pan", (0.0, 0.0))
    if region is None:
        return (zoom_u, pan_u)
    w0, h0 = _refboard_ref_size(scene, region)
    k = max(1e-4, float(region.height) / h0)
    pan = (region.width * 0.5 / w0 - 0.5 + pan_u[0] * k,
           region.height * 0.5 / h0 - 0.5 + pan_u[1] * k)
    return (zoom_u * k, pan, w0, h0)


def _refboard_canvas_view(scene, region):
    """Identity view in canvas space, carrying the canvas dims. Drag math runs
    here so deltas are immune to pan/zoom and to viewport size."""
    w0, h0 = _refboard_ref_size(scene, region)
    return (1.0, (0.0, 0.0), w0, h0)


def _refboard_view_dims(view, region):
    """Canvas dims a view is expressed in, falling back to the live region."""
    if len(view) >= 4 and view[2] and view[3]:
        return float(view[2]), float(view[3])
    return float(region.width), float(region.height)


# Ambient pin candidates: region size snapshots waiting to prove they are a
# settled viewport and not a mid-resize transient.
_refboard_ref_pending = {}


def _refboard_ensure_ref_size(scene, region, stable=False):
    """Pin the canvas size once, so later resizes cannot restretch the layout.

    Only meaningful with refs on the board: an empty board has no layout to
    preserve, and pinning early would freeze the canvas to whatever viewport
    happened to be focused first.

    stable=False pins immediately - used by authoring gestures (paste, drag
    start) where the current size is by definition the authoring size.
    stable=True (ambient callers like the draw callback or modal events)
    only commits a size that has held steady for a moment: during a window
    border drag the region reports intermediate sizes every frame, and
    pinning one of those would blow the layout up by a huge factor.
    """
    if not len(getattr(scene, "refboard_items", None) or []):
        return
    rs = getattr(scene, "refboard_ref_size", None)
    if rs is not None and rs[0] >= 64.0 and rs[1] >= 64.0:
        return
    w, h = float(region.width), float(region.height)
    if w < 64.0 or h < 64.0:
        return
    if not stable:
        scene.refboard_ref_size = (w, h)
        return
    key = scene.as_pointer()
    now = time.time()
    cand = _refboard_ref_pending.get(key)
    if cand is None or abs(cand[0] - w) > 4.0 or abs(cand[1] - h) > 4.0:
        _refboard_ref_pending[key] = (w, h, now)
        return
    if now - cand[2] >= 0.4:
        scene.refboard_ref_size = (w, h)
        _refboard_ref_pending.pop(key, None)


def _refboard_zoom_anchor(scene, region, sx, sy, cx, cy, zoom_new):
    """Set zoom while keeping canvas point (cx,cy) under screen point (sx,sy).

    Inverts screen = viewport_center + pan*k*canvas + (c - canvas/2)*zoom*k.
    """
    w0, h0 = _refboard_ref_size(scene, region)
    k = _refboard_fit(scene, region)
    scene.refboard_view_pan = (
        (sx - region.width * 0.5 - (cx - 0.5 * w0) * zoom_new * k) / (k * w0),
        (sy - region.height * 0.5 - (cy - 0.5 * h0) * zoom_new * k) / (k * h0))
    scene.refboard_view_zoom = zoom_new


def _refboard_canvas_mode(scene):
    """Explicit edit mode for the board (Alt+` enters it): while on, images are
    interactive and 3D-scene input is swallowed."""
    if not _refboard_canvas_on:
        return False
    items = getattr(scene, "refboard_items", None)
    return items is not None and len(items) > 0


def _refboard_enter_object_mode():
    """Drop the active object back to Object Mode when Refboard edit mode
    starts. Object-mode undo is the memfile stack our undo_post guard
    re-pins against; Sculpt Mode has its own non-memfile undo that shares
    the same Ctrl+Z binding, so leaving it active in the background makes
    swallowed keys ambiguous - a Z that ever falls through steps sculpt
    history instead of board history."""
    obj = getattr(bpy.context, "active_object", None)
    if obj is None or getattr(obj, "mode", "OBJECT") == 'OBJECT':
        return
    try:
        with bpy.context.temp_override(
                object=obj, active_object=obj):
            bpy.ops.object.mode_set(mode='OBJECT')
    except Exception:
        pass


def _refboard_switch_mode(scene, alt):
    """The ` / Alt+` mode switch, shared by the modal and the keymap items so
    the two can never drift apart.

    Bare ` shows/hides the board. Alt+` enters edit mode, revealing the board
    first if it was hidden, so the shortcut always lands somewhere editable.
    Either key leaves edit mode when it is on, which is why neither is a plain
    toggle of its own state.
    """
    global _refboard_canvas_on, _refboard_mode_ts, _refboard_mode_label
    global _refboard_help_ts
    if _refboard_canvas_on:
        _refboard_canvas_on = False
        _refboard_npanel_restore()
        scene.refboard_selected = -1
        _refboard_group.clear()
        scene.refboard_all_hidden = False
        _refboard_mode_label = "Refboard Exit"
    elif alt:
        scene.refboard_all_hidden = False
        _refboard_enter_object_mode()
        _refboard_canvas_on = True
        _refboard_npanel_hide()
        _refboard_undo_seed(scene)
        _refboard_help_ts = time.time()
        _refboard_mode_label = "Refboard Edit"
    else:
        hidden = not getattr(scene, "refboard_all_hidden", False)
        scene.refboard_all_hidden = hidden
        if hidden:
            # A hidden board must not keep an active selection, or the next
            # edit-mode entry would start with an invisible ref selected.
            scene.refboard_selected = -1
        _refboard_mode_label = "Refboard Off" if hidden else "Refboard Exit"
    _refboard_mode_ts = time.time()
    return _refboard_mode_label


def _refboard_to_canvas_px(region, scene, mx, my):
    """Screen px -> canvas px (the space item.pos and item.scale live in)."""
    view = _refboard_view(scene, region)
    zoom, pan = view[0], view[1]
    cw, ch = _refboard_view_dims(view, region)
    return ((mx - (0.5 + pan[0]) * cw) / zoom + 0.5 * cw,
            (my - (0.5 + pan[1]) * ch) / zoom + 0.5 * ch)


def _refboard_crop_uvs(item, crop=None):
    """Visible-region corner UVs in BL,BR,TR,TL order. `crop` overrides the
    stored quad with an axis-aligned rect (used for drag baselines)."""
    if crop is not None:
        l, b, r, t = crop
        return [(l, b), (r, b), (r, t), (l, t)]
    p = item.crop_pts
    return [(p[0], p[1]), (p[2], p[3]), (p[4], p[5]), (p[6], p[7])]


def _refboard_crop_rect_update(item):
    """Direct writes to `crop` reset the quad to the axis-aligned rect."""
    l, b, r, t = item.crop
    item.crop_pts = (l, b, r, b, r, t, l, t)


def _refboard_crop_commit(item, pts):
    """Store a crop quad and keep `crop` (the bookkeeping AABB) in sync."""
    us = [pts[0][0], pts[1][0], pts[2][0], pts[3][0]]
    vs = [pts[0][1], pts[1][1], pts[2][1], pts[3][1]]
    # `crop` first: its update callback overwrites crop_pts with the plain
    # rect, then the real quad goes in last so it wins.
    item.crop = (min(us), min(vs), max(us), max(vs))
    item.crop_pts = tuple(v for p in pts for v in p)


def _refboard_flip_uvs(item, uvs):
    """Mirror the quad UVs per the flip flags. Corner order is BL,BR,TR,TL:
    a horizontal flip swaps 0<->1 and 3<->2, vertical swaps 0<->3 and
    1<->2. Only the texcoords are permuted - quad positions are unchanged,
    so the crop window stays put while the content mirrors."""
    if item.flip_x:
        uvs = [uvs[1], uvs[0], uvs[3], uvs[2]]
    if item.flip_y:
        uvs = [uvs[3], uvs[2], uvs[1], uvs[0]]
    return uvs


def _refboard_uv_to_screen(item, region, view, iw, ih, u, v):
    """One UV point -> screen/canvas px through the item transform."""
    zoom, pan = view[0], view[1]
    cw, ch = _refboard_view_dims(view, region)
    lx = (u - 0.5) * iw * item.scale[0] * zoom
    ly = (v - 0.5) * ih * item.scale[1] * zoom
    px = ((item.pos[0] - 0.5) * zoom + 0.5 + pan[0]) * cw
    py = ((item.pos[1] - 0.5) * zoom + 0.5 + pan[1]) * ch
    c, s = math.cos(item.rotation), math.sin(item.rotation)
    return (px + lx * c - ly * s, py + lx * s + ly * c)


def _refboard_crop_quad(item, region, img, view=_REFBOARD_VIEW_IDENT):
    """Exact on-screen quad of the visible crop region (handles rotated
    crops, which need not be axis-aligned in the item frame)."""
    iw, ih = img.size[0], img.size[1]
    return [_refboard_uv_to_screen(item, region, view, iw, ih, u, v)
            for u, v in _refboard_crop_uvs(item)]


def _refboard_rect(item, region, img, view=_REFBOARD_VIEW_IDENT):
    """Interaction rect fitted to the visible crop quad: (center offset
    cx,cy, half-size hx,hy) in the rect's own rotated frame, plus the
    screen center (px,py) and rotation. For axis-aligned crops this is
    exactly the crop rect in the item frame; for rotated crops it is the
    best-fit rect of the transformed quad. `view` applies the canvas
    pan/zoom; drag math passes the identity view (canvas space)."""
    iw, ih = img.size[0], img.size[1]
    if iw < 1 or ih < 1:
        return None
    zoom, pan = view[0], view[1]
    cw, ch = _refboard_view_dims(view, region)
    sx, sy = item.scale[0] * zoom, item.scale[1] * zoom
    px = ((item.pos[0] - 0.5) * zoom + 0.5 + pan[0]) * cw
    py = ((item.pos[1] - 0.5) * zoom + 0.5 + pan[1]) * ch
    c, s = math.cos(item.rotation), math.sin(item.rotation)
    sp = []
    for u, v in _refboard_crop_uvs(item):
        lx, ly = (u - 0.5) * iw * sx, (v - 0.5) * ih * sy
        sp.append((px + lx * c - ly * s, py + lx * s + ly * c))
    e0x, e0y = sp[1][0] - sp[0][0], sp[1][1] - sp[0][1]
    e1x, e1y = sp[2][0] - sp[1][0], sp[2][1] - sp[1][1]
    hx = max(0.5, math.hypot(e0x, e0y) * 0.5)
    hy = max(0.5, math.hypot(e1x, e1y) * 0.5)
    rot = math.atan2(e0y, e0x) if e0x * e0x + e0y * e0y > 1e-8 \
        else item.rotation
    cc, ss = math.cos(-rot), math.sin(-rot)
    dx = (sp[0][0] + sp[2][0]) * 0.5 - px
    dy = (sp[0][1] + sp[2][1]) * 0.5 - py
    return (dx * cc - dy * ss, dx * ss + dy * cc, hx, hy, px, py, rot)


def _refboard_to_local(rect, mx, my):
    cx, cy, hx, hy, px, py, rot = rect
    dx, dy = mx - px, my - py
    c, s = math.cos(-rot), math.sin(-rot)
    return (dx * c - dy * s, dx * s + dy * c)


def _refboard_to_screen(rect, lx, ly):
    cx, cy, hx, hy, px, py, rot = rect
    c, s = math.cos(rot), math.sin(rot)
    return (px + lx * c - ly * s, py + lx * s + ly * c)


def _refboard_corners_local(rect):
    cx, cy, hx, hy = rect[0], rect[1], rect[2], rect[3]
    return [(cx - hx, cy - hy), (cx + hx, cy - hy),
            (cx + hx, cy + hy), (cx - hx, cy + hy)]


def _refboard_quad(rect):
    return [_refboard_to_screen(rect, lx, ly)
            for lx, ly in _refboard_corners_local(rect)]


def _refboard_zone(rect, mx, my, handles, crop_mode=False):
    """Classify a point against one item. With handles=True (selected item),
    handles/rotate zones are tested before the interior. crop_mode (Ctrl
    held) suppresses dots/rotate and activates the border crop strips."""
    lx, ly = _refboard_to_local(rect, mx, my)
    cx, cy, hx, hy = rect[0], rect[1], rect[2], rect[3]
    corners = _refboard_corners_local(rect)
    if handles:
        if crop_mode:
            # Ctrl held: scale dots and the rotate band are suppressed -
            # the border strips are the only special zones (crop edges).
            in_x = abs(lx - cx) <= hx + _REFBOARD_BORDER_HIT
            in_y = abs(ly - cy) <= hy + _REFBOARD_BORDER_HIT
            on_border = in_x and in_y and (
                abs(lx - cx) > hx - _REFBOARD_BORDER_HIT or
                abs(ly - cy) > hy - _REFBOARD_BORDER_HIT)
            if on_border:
                if abs(abs(lx - cx) - hx) <= _REFBOARD_BORDER_HIT and \
                        abs(ly - cy) <= hy:
                    return ('crop', 2 if lx > cx else 0)
                if abs(abs(ly - cy) - hy) <= _REFBOARD_BORDER_HIT and \
                        abs(lx - cx) <= hx:
                    return ('crop', 3 if ly > cy else 1)
        else:
            for i, (hxp, hyp) in enumerate(corners):
                if math.hypot(lx - hxp, ly - hyp) <= _REFBOARD_HANDLE_HIT:
                    return ('corner', i)
            mids = [((corners[0][0] + corners[1][0]) * 0.5,
                     (corners[0][1] + corners[1][1]) * 0.5),
                    ((corners[1][0] + corners[2][0]) * 0.5,
                     (corners[1][1] + corners[2][1]) * 0.5),
                    ((corners[2][0] + corners[3][0]) * 0.5,
                     (corners[2][1] + corners[3][1]) * 0.5),
                    ((corners[3][0] + corners[0][0]) * 0.5,
                     (corners[3][1] + corners[0][1]) * 0.5)]
            for i, (mxp, myp) in enumerate(mids):
                if math.hypot(lx - mxp, ly - myp) <= _REFBOARD_HANDLE_HIT:
                    return ('edge', i)  # 0 bottom,1 right,2 top,3 left
            # Rotate only when the point is outside the quad entirely AND
            # well past a corner (_REFBOARD_ROT_IN) - never near a dot.
            if abs(lx - cx) > hx or abs(ly - cy) > hy:
                for i, (hxp, hyp) in enumerate(corners):
                    d = math.hypot(lx - hxp, ly - hyp)
                    if _REFBOARD_ROT_IN <= d <= _REFBOARD_ROT_OUT:
                        return ('rotate', i)
    if abs(lx - cx) <= hx and abs(ly - cy) <= hy:
        return ('inside', -1)
    return None


def _refboard_group_rect(scene, region):
    """Canvas-space axis-aligned bbox of the marquee group, as a rect tuple
    (0,0,hx,hy,px,py,0) so zone/pick math works on it like a virtual ref."""
    xs, ys = [], []
    for i in _refboard_group:
        if not (0 <= i < len(scene.refboard_items)):
            continue
        item = scene.refboard_items[i]
        img = item.image
        if img is None or not item.visible or item.locked:
            continue
        rect = _refboard_rect(item, region, img,
                              _refboard_canvas_view(scene, region))
        if rect is None:
            continue
        for lx, ly in _refboard_corners_local(rect):
            sx, sy = _refboard_to_screen(rect, lx, ly)
            xs.append(sx)
            ys.append(sy)
    if not xs:
        return None
    return (0.0, 0.0,
            max(0.5, (max(xs) - min(xs)) * 0.5),
            max(0.5, (max(ys) - min(ys)) * 0.5),
            (min(xs) + max(xs)) * 0.5,
            (min(ys) + max(ys)) * 0.5, 0.0)


def _refboard_group_rect_screen(scene, region):
    """The group bbox mapped through the canvas view into screen pixels."""
    rect = _refboard_group_rect(scene, region)
    if rect is None:
        return None
    view = _refboard_view(scene, region)
    zoom, pan = view[0], view[1]
    cw, ch = _refboard_view_dims(view, region)
    px = (rect[4] - 0.5 * cw) * zoom + (0.5 + pan[0]) * cw
    py = (rect[5] - 0.5 * ch) * zoom + (0.5 + pan[1]) * ch
    return (rect[0] * zoom, rect[1] * zoom, rect[2] * zoom,
            rect[3] * zoom, px, py, rect[6])


def _refboard_point_in_quad(px, py, q):
    """Convex-quad point test via consistent edge-winding signs."""
    s = None
    for i in range(len(q)):
        a, b = q[i], q[(i + 1) % len(q)]
        cross = (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0])
        if s is None:
            s = cross >= 0.0
        elif (cross >= 0.0) != s:
            return False
    return True


def _refboard_make_dominant(scene, region, mouse):
    """Scale the group member under `mouse` (screen px) so it clearly reads
    as the dominant ref: grown until it fills ~75% of the group bbox height
    (or ~60% of the width), aspect preserved. Never shrinks. Returns the
    member index or None."""
    if not mouse:
        return None
    view = _refboard_view(scene, region)
    hit_i = None
    for i in _refboard_group:
        if not (0 <= i < len(scene.refboard_items)):
            continue
        it = scene.refboard_items[i]
        img = it.image
        if img is None:
            continue
        rect = _refboard_rect(it, region, img, view)
        if rect is not None and \
                _refboard_point_in_quad(mouse[0], mouse[1],
                                       _refboard_quad(rect)):
            hit_i = i
            break
    if hit_i is None:
        return None
    grect = _refboard_group_rect_screen(scene, region)
    if grect is None:
        return None
    it = scene.refboard_items[hit_i]
    rect = _refboard_rect(it, region, it.image, view)
    if rect is None:
        return None
    q = _refboard_quad(rect)
    xs = [p[0] for p in q]
    ys = [p[1] for p in q]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    if w < 1e-4 or h < 1e-4:
        return None
    f = min(grect[3] * 2.0 * 0.75 / h, grect[2] * 2.0 * 0.60 / w)
    if f > 1.0:
        it.scale = (it.scale[0] * f, it.scale[1] * f)
    return hit_i


def _refboard_arrange_auto(scene, region, pad=6.0):
    """Shelf-pack the marquee group's refs inside the current group bbox:
    repositions only (no scaling), ~pad px gap between items, tallest
    first, rows centered inside the bbox."""
    idxs = [i for i in _refboard_group if 0 <= i < len(scene.refboard_items)]
    if len(idxs) < 2:
        return False
    grect = _refboard_group_rect(scene, region)
    if grect is None:
        return False
    rw, rh = _refboard_ref_size(scene, region)
    bw, bh = grect[2] * 2.0, grect[3] * 2.0
    bx0, by0 = grect[4] - grect[2], grect[5] - grect[3]
    # Per-item canvas-space AABB sizes (rotation included).
    dims = []
    for i in idxs:
        it = scene.refboard_items[i]
        img = it.image
        if img is None:
            continue
        rect = _refboard_rect(it, region, img)
        if rect is None:
            continue
        q = _refboard_quad(rect)
        xs = [p[0] for p in q]
        ys = [p[1] for p in q]
        # The pos anchor is the UNCROPPED image center; the visible quad
        # center is off by the (rotated) crop offset - carry it so the
        # placed pos centers the visible chunk, not the latent frame.
        c, s = math.cos(it.rotation), math.sin(it.rotation)
        dims.append((i, max(xs) - min(xs) + pad, max(ys) - min(ys) + pad,
                     rect[0] * c - rect[1] * s,
                     rect[0] * s + rect[1] * c))
    if len(dims) < 2:
        return False
    dims.sort(key=lambda d: -d[2])
    shelves, cur, x = [], [], 0.0
    for d in dims:
        if cur and x + d[1] > bw:
            shelves.append(cur)
            cur, x = [], 0.0
        cur.append(d)
        x += d[1]
    if cur:
        shelves.append(cur)
    total_h = sum(max(d[2] for d in s) for s in shelves)
    y = by0 + (bh + total_h) * 0.5  # block vertically centered on the bbox
    for s in shelves:
        sh = max(d[2] for d in s)
        sw = sum(d[1] for d in s)
        x = grect[4] - sw * 0.5     # each row horizontally centered
        for i, w, h, ox, oy in s:
            it = scene.refboard_items[i]
            it.pos = ((x + w * 0.5 - ox) / rw, (y - sh * 0.5 - oy) / rh)
            x += w
        y -= sh
    return True


def _refboard_screen_to_uv(item, region, scene, mx, my):
    """Screen px -> image UV in the item's frame (pos anchors the
    uncropped image center; unrotate, unscale, then normalize)."""
    img = item.image
    iw, ih = float(img.size[0]), float(img.size[1])
    cmx, cmy = _refboard_to_canvas_px(region, scene, mx, my)
    cw, ch = _refboard_ref_size(scene, region)
    mdx = cmx - item.pos[0] * cw
    mdy = cmy - item.pos[1] * ch
    c, s = math.cos(-item.rotation), math.sin(-item.rotation)
    lx = mdx * c - mdy * s
    ly = mdx * s + mdy * c
    return (lx / max(1e-4, iw * item.scale[0]) + 0.5,
            ly / max(1e-4, ih * item.scale[1]) + 0.5)


def _refboard_point_in_poly(px, py, poly):
    """Point inside a convex polygon, edge signs taken from the centroid."""
    cx = sum(pt[0] for pt in poly) / len(poly)
    cy = sum(pt[1] for pt in poly) / len(poly)
    for i in range(len(poly)):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % len(poly)]
        ex, ey = bx - ax, by - ay
        el = math.hypot(ex, ey)
        if el < 1e-12:
            continue
        side = ex * (cy - ay) - ey * (cx - ax)
        if side == 0.0:
            continue
        # Cross products scale with |edge|^2 for big quads - normalize to
        # a signed pixel distance so the tolerance is real (0.001 px).
        val = (ex * (py - ay) - ey * (px - ax)) / el
        if (1.0 if side > 0.0 else -1.0) * val < -0.001:
            return False
    return True


def _refboard_rect_poly_overlap(xa, ya, xb, yb, poly):
    """SAT overlap: axis-aligned rect vs a convex polygon (screen px)."""
    rect = [(xa, ya), (xb, ya), (xb, yb), (xa, yb)]
    for ax_ in (0, 1):
        rlo, rhi = xa, xb
        if ax_ == 1:
            rlo, rhi = ya, yb
        plo = min(pt[ax_] for pt in poly)
        phi = max(pt[ax_] for pt in poly)
        if rhi < plo or phi < rlo:
            return False
    for i in range(len(poly)):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % len(poly)]
        nx, ny = ay - by, bx - ax
        rp = [pt[0] * nx + pt[1] * ny for pt in rect]
        pp = [pt[0] * nx + pt[1] * ny for pt in poly]
        if max(rp) < min(pp) or max(pp) < min(rp):
            return False
    return True


def _refboard_quad_axisaligned(quad):
    """True when every edge of the screen quad is horizontal or vertical -
    i.e. the visible crop is a plain rect (unrotated image, unsheared
    crop)."""
    for i in range(4):
        ax, ay = quad[i]
        bx, by = quad[(i + 1) % 4]
        if abs(ax - bx) > 0.25 and abs(ay - by) > 0.25:
            return False
    return True


def _refboard_marquee_clamp(m0, mx, my, quad):
    """Scale the m0->cursor vector so the axis-aligned marquee anchored at
    m0 never leaves the convex quad: the first rect corner to touch an edge
    stops the drag. Every moving corner is linear in t, so the limit is a
    closed-form solve per edge."""
    dx, dy = mx - m0[0], my - m0[1]
    if dx == 0.0 and dy == 0.0:
        return m0
    cx = sum(pq[0] for pq in quad) / 4.0
    cy = sum(pq[1] for pq in quad) / 4.0
    tmax = 1.0
    for i in range(4):
        ax, ay = quad[i]
        bx, by = quad[(i + 1) % 4]
        ex, ey = bx - ax, by - ay
        sgn = ex * (cy - ay) - ey * (cx - ax)
        if abs(sgn) < 1e-12:
            continue
        sgn = 1.0 if sgn > 0.0 else -1.0
        a = (ex * (m0[1] - ay) - ey * (m0[0] - ax)) * sgn
        if a < -1e-9:
            return m0      # anchor outside this wall: no valid rect
        for ox, oy in ((dx, 0.0), (0.0, dy), (dx, dy)):
            f1 = (ex * oy - ey * ox) * sgn
            if f1 < -1e-12:
                tmax = min(tmax, a / -f1)
    tmax = max(0.0, min(1.0, tmax))
    return (m0[0] + dx * tmax, m0[1] + dy * tmax)


def _refboard_cropmarq_update(scene, region, mx, my):
    """Per-move marquee update. An inside-start marquee is clamped into the
    item's crop quad; an outside-start marquee probes every visible ref -
    overlapping a rotated (non-axis-aligned) crop flags the drag illegal so
    the draw paints it red and the release discards it."""
    cm = _refboard_cropmarq
    if cm is None:
        return
    m0 = cm["m0"]
    view = _refboard_view(scene, region)
    items = scene.refboard_items
    if cm.get("inside"):
        cm["cur"] = (mx, my)
        idx = cm.get("idx")
        if idx is not None and 0 <= idx < len(items):
            item = items[idx]
            if item.image is not None:
                quad = _refboard_crop_quad(item, region, item.image, view)
                cm["cur"] = _refboard_marquee_clamp(m0, mx, my, quad)
        return
    cm["cur"] = (mx, my)
    xa, xb = min(m0[0], mx), max(m0[0], mx)
    ya, yb = min(m0[1], my), max(m0[1], my)
    illegal = False
    target = None
    for i, item in enumerate(items):
        img = item.image
        if img is None or not item.visible or item.locked:
            continue
        quad = _refboard_crop_quad(item, region, img, view)
        if not _refboard_rect_poly_overlap(xa, ya, xb, yb, quad):
            continue
        if not _refboard_quad_axisaligned(quad):
            illegal = True
        else:
            target = i            # collection order = draw order: last wins
    cm["illegal"] = illegal
    cm["target"] = target


def _refboard_apply_crop_rect(scene, region, idx, x0, y0, x1, y1):
    """Crop item `idx` to the axis-aligned screen rect drawn by the crop
    marquee. The screen rect maps to a (possibly rotated) rect in UV space;
    the image's own rotation is untouched - for a rotated image the crop
    is stored verbatim as a UV quad (crop_pts), so the visible window is
    exactly the screen-aligned marquee while the pixels keep their angle."""
    if not (0 <= idx < len(scene.refboard_items)):
        return False
    item = scene.refboard_items[idx]
    if item.image is None:
        return False
    pts = [_refboard_screen_to_uv(item, region, scene, sx, sy)
           for sx, sy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    # Marquee fully on the image: the mapped UV quad renders back as
    # exactly the drawn rect, even when it is sheared in UV space.
    if all(0.0 <= u <= 1.0 and 0.0 <= v <= 1.0 for u, v in pts):
        _refboard_crop_commit(item, pts)
        return True
    # Partly off-image: clip the quad against the unit square
    # (Sutherland-Hodgman) and fit a rotated rect to what is left.
    e0x, e0y = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
    cr = math.atan2(e0y, e0x) if e0x * e0x + e0y * e0y > 1e-12 else 0.0
    poly = pts[:]
    for axis, val, keep_gt in ((0, 0.0, True), (0, 1.0, False),
                               (1, 0.0, True), (1, 1.0, False)):
        out = []
        for i in range(len(poly)):
            a, b = poly[i - 1], poly[i]
            ain = (a[axis] >= val) if keep_gt else (a[axis] <= val)
            bin_ = (b[axis] >= val) if keep_gt else (b[axis] <= val)
            if ain != bin_:
                da, db = a[axis] - val, b[axis] - val
                tt = da / (da - db)
                out.append((a[0] + (b[0] - a[0]) * tt,
                            a[1] + (b[1] - a[1]) * tt))
            if bin_:
                out.append(b)
        poly = out
        if not poly:
            return False
    # Symmetric extents about the centroid keep the fitted rect centred on
    # the clipped region.
    cu = sum(p[0] for p in poly) / len(poly)
    cv = sum(p[1] for p in poly) / len(poly)
    cc, ss = math.cos(-cr), math.sin(-cr)
    hw = hh = 0.0
    for u, v in poly:
        du, dv = u - cu, v - cv
        hw = max(hw, abs(du * cc - dv * ss))
        hh = max(hh, abs(du * ss + dv * cc))
    if hw < 0.0025 or hh < 0.0025:
        return False
    corners = [(cu - hw, cv - hh), (cu + hw, cv - hh),
               (cu + hw, cv + hh), (cu - hw, cv + hh)]
    cc, ss = math.cos(cr), math.sin(cr)
    mru, mrv = cu, cv
    _refboard_crop_commit(item, [
        (mru + (u - mru) * cc - (v - mrv) * ss,
         mrv + (u - mru) * ss + (v - mrv) * cc) for u, v in corners])
    return True


def _refboard_marquee_members(scene, region, x0, y0, x1, y1):
    """Indices of refs whose screen-space bounds intersect the marquee."""
    xa, xb = min(x0, x1), max(x0, x1)
    ya, yb = min(y0, y1), max(y0, y1)
    members = []
    view = _refboard_view(scene, region)
    for i, item in enumerate(scene.refboard_items):
        img = item.image
        if img is None or not item.visible or item.locked:
            continue
        rect = _refboard_rect(item, region, img, view)
        if rect is None:
            continue
        quad = _refboard_quad(rect)
        xs = [q[0] for q in quad]
        ys = [q[1] for q in quad]
        if max(xs) < xa or min(xs) > xb or \
                max(ys) < ya or min(ys) > yb:
            continue
        members.append(i)
    return members


def _refboard_crop_edge_zone(item, region, view, mx, my):
    """Ctrl-mode edge pick against the real crop quad: the nearest edge
    segment within the hit band wins. On a skewed quad (rotated image,
    marquee-cropped) the fitted rect's edge strips diverge from the drawn
    edges - hovering the visible top edge could land on the bottom strip.
    Hit-testing the quad itself keeps the hovered edge honest."""
    img = item.image
    if img is None:
        return None
    quad = _refboard_crop_quad(item, region, img, view)
    best = None
    for i in range(4):
        ax, ay = quad[i]
        bx, by = quad[(i + 1) % 4]
        ex, ey = bx - ax, by - ay
        el2 = ex * ex + ey * ey
        t = 0.0 if el2 < 1e-12 else max(0.0, min(1.0,
            ((mx - ax) * ex + (my - ay) * ey) / el2))
        ddx = mx - (ax + ex * t)
        ddy = my - (ay + ey * t)
        dd = ddx * ddx + ddy * ddy
        if dd <= _REFBOARD_BORDER_HIT * _REFBOARD_BORDER_HIT and \
                (best is None or dd < best[0]):
            best = (dd, i)
    if best is None:
        return None
    # Edge i joins pts[i]-pts[i+1]: 0-1 bottom, 1-2 right, 2-3 top, 3-0
    # left - matching _refboard_zone's sub numbering.
    return ('crop', (best[1] + 1) % 4)


def _refboard_pick(scene, region, mx, my, ctrl=False):
    """Returns (index, zone, sub) for the topmost hit, or None. index is the
    string 'group' when the marquee bbox was hit. ctrl enters crop mode:
    edges on the selected ref become crop zones, dots/rotate suppressed."""
    if getattr(scene, "refboard_all_hidden", False):
        return None
    items = scene.refboard_items
    sel = scene.refboard_selected
    view = _refboard_view(scene, region)
    if _refboard_group:
        grect = _refboard_group_rect_screen(scene, region)
        if grect is not None:
            z = _refboard_zone(grect, mx, my, True)
            if z is not None:
                # The border strip maps to crop for single refs; a group
                # has no crop, treat it as the move zone.
                return ('group',
                        'inside' if z[0] == 'crop' else z[0], z[1])
    if 0 <= sel < len(items):
        item = items[sel]
        img = item.image
        if img is not None and item.visible and not item.locked:
            rect = _refboard_rect(item, region, img, view)
            if rect is not None:
                if ctrl:
                    z = _refboard_crop_edge_zone(item, region, view,
                                                 mx, my)
                    if z is None:
                        z = _refboard_zone(rect, mx, my, False)
                else:
                    z = _refboard_zone(rect, mx, my, True, ctrl)
                if z is not None:
                    return (sel, z[0], z[1])
    for i in range(len(items) - 1, -1, -1):
        if i == sel:
            continue
        item = items[i]
        img = item.image
        if img is None or not item.visible or item.locked:
            continue
        rect = _refboard_rect(item, region, img, view)
        if rect is None:
            continue
        z = _refboard_zone(rect, mx, my, False)
        if z is not None:
            return (i, z[0], z[1])
    return None


def _refboard_gp_active(context):
    """True while a grease-pencil draw/annotate tool is active or we're in a
    GP mode: ref images must never eat strokes drawn over them."""
    mode = getattr(context, "mode", "") or ""
    if 'GPENCIL' in mode or 'GREASE_PENCIL' in mode:
        return True
    try:
        tool = context.workspace.tools.from_space_view3d_mode(mode)
        idn = (getattr(tool, "idname", "") or "").lower()
        return 'annotate' in idn or 'draw' in idn
    except Exception:
        return False


# --- GPU drawing --------------------------------------------------------------

def _refboard_mvp():
    try:
        return gpu.matrix.get_mvp_matrix()
    except Exception:
        return gpu.matrix.get_projection_matrix() @ \
            gpu.matrix.get_model_view_matrix()


def _refboard_shaders():
    """Lazily build both shaders. create_from_info is the 5.x path;
    from_builtin is the fallback for older Blenders."""
    global _REFBOARD_TEX_SHADER, _REFBOARD_TEX_SHADER_NEEDS_MVP
    global _REFBOARD_FLAT_SHADER, _REFBOARD_FLAT_SHADER_NEEDS_MVP
    global _REFBOARD_SMOOTH_SHADER, _REFBOARD_SMOOTH_SHADER_NEEDS_MVP
    if gpu is None:
        return None, None
    if _REFBOARD_TEX_SHADER is None:
        try:
            vsi = gpu.types.GPUStageInterfaceInfo("refboard_iface")
            vsi.smooth("VEC2", "uvInterp")
            ci = gpu.types.GPUShaderCreateInfo()
            ci.vertex_in(0, "VEC2", "pos")
            ci.vertex_in(1, "VEC2", "texCoord")
            ci.vertex_out(vsi)
            ci.sampler(0, "FLOAT_2D", "image")
            ci.push_constant("MAT4", "ModelViewProjectionMatrix")
            ci.push_constant("FLOAT", "opacity")
            ci.fragment_out(0, "VEC4", "fragColor")
            ci.vertex_source(
                "void main(){ uvInterp = texCoord; gl_Position = "
                "ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0); }")
            ci.fragment_source(
                "void main(){ fragColor = texture(image, uvInterp) * "
                "vec4(opacity, opacity, opacity, opacity); }")
            _REFBOARD_TEX_SHADER = gpu.shader.create_from_info(ci)
            _REFBOARD_TEX_SHADER_NEEDS_MVP = True
        except Exception:
            try:
                _REFBOARD_TEX_SHADER = gpu.shader.from_builtin('IMAGE')
                _REFBOARD_TEX_SHADER_NEEDS_MVP = False
            except Exception:
                _REFBOARD_TEX_SHADER = None
    if _REFBOARD_FLAT_SHADER is None:
        try:
            ci = gpu.types.GPUShaderCreateInfo()
            ci.vertex_in(0, "VEC2", "pos")
            ci.push_constant("MAT4", "ModelViewProjectionMatrix")
            ci.push_constant("VEC4", "color")
            ci.fragment_out(0, "VEC4", "fragColor")
            ci.vertex_source(
                "void main(){ gl_Position = ModelViewProjectionMatrix * "
                "vec4(pos, 0.0, 1.0); }")
            ci.fragment_source("void main(){ fragColor = color; }")
            _REFBOARD_FLAT_SHADER = gpu.shader.create_from_info(ci)
            _REFBOARD_FLAT_SHADER_NEEDS_MVP = True
        except Exception:
            try:
                _REFBOARD_FLAT_SHADER = gpu.shader.from_builtin('UNIFORM_COLOR')
                _REFBOARD_FLAT_SHADER_NEEDS_MVP = False
            except Exception:
                _REFBOARD_FLAT_SHADER = None
    if _REFBOARD_SMOOTH_SHADER is None:
        # Per-vertex color shader so handles/borders can carry an alpha
        # falloff and antialias properly.
        try:
            vsi = gpu.types.GPUStageInterfaceInfo("refboard_smooth_iface")
            vsi.smooth("VEC4", "colorInterp")
            ci = gpu.types.GPUShaderCreateInfo()
            ci.vertex_in(0, "VEC2", "pos")
            ci.vertex_in(1, "VEC4", "color")
            ci.vertex_out(vsi)
            ci.push_constant("MAT4", "ModelViewProjectionMatrix")
            ci.fragment_out(0, "VEC4", "fragColor")
            ci.vertex_source(
                "void main(){ colorInterp = color; gl_Position = "
                "ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0); }")
            ci.fragment_source("void main(){ fragColor = colorInterp; }")
            _REFBOARD_SMOOTH_SHADER = gpu.shader.create_from_info(ci)
            _REFBOARD_SMOOTH_SHADER_NEEDS_MVP = True
        except Exception:
            try:
                _REFBOARD_SMOOTH_SHADER = gpu.shader.from_builtin(
                    'SMOOTH_COLOR')
                _REFBOARD_SMOOTH_SHADER_NEEDS_MVP = False
            except Exception:
                _REFBOARD_SMOOTH_SHADER = None
    return _REFBOARD_TEX_SHADER, _REFBOARD_FLAT_SHADER


def _refboard_draw_flat(flat, mode, pts, rgba):
    batch = batch_for_shader(flat, mode, {"pos": pts})
    flat.bind()
    if _REFBOARD_FLAT_SHADER_NEEDS_MVP:
        flat.uniform_float("ModelViewProjectionMatrix", _refboard_mvp())
        flat.uniform_float("color", rgba)
    else:
        flat.uniform_float("color", rgba)
    batch.draw(flat)


def _refboard_draw_smooth(mode, pts, cols):
    smooth = _REFBOARD_SMOOTH_SHADER
    if smooth is None:
        return
    batch = batch_for_shader(smooth, mode, {"pos": pts, "color": cols})
    smooth.bind()
    if _REFBOARD_SMOOTH_SHADER_NEEDS_MVP:
        smooth.uniform_float("ModelViewProjectionMatrix", _refboard_mvp())
    batch.draw(smooth)


def _refboard_disc(x, y, r, rgba):
    """Antialiased filled dot: a solid core fan plus a 1.25px alpha-falloff
    ring, so the edge reads smooth instead of stair-stepped. The fade verts
    are (0,0,0,0) because ALPHA_PREMULT expects premultiplied colors — a
    colored zero-alpha vertex adds a bright fuzzy halo."""
    seg = 36
    ri = max(0.5, r - 0.75)
    ro = r + 1.25
    solid = (rgba[0], rgba[1], rgba[2], rgba[3])
    fade = (0.0, 0.0, 0.0, 0.0)
    pts = [(x, y)]
    cols = [solid]
    for i in range(seg + 1):
        a = 2.0 * math.pi * i / seg
        pts.append((x + ri * math.cos(a), y + ri * math.sin(a)))
        cols.append(solid)
    _refboard_draw_smooth('TRI_FAN', pts, cols)
    pts = []
    cols = []
    for i in range(seg + 1):
        a = 2.0 * math.pi * i / seg
        pts.append((x + ri * math.cos(a), y + ri * math.sin(a)))
        cols.append(solid)
        pts.append((x + ro * math.cos(a), y + ro * math.sin(a)))
        cols.append(fade)
    _refboard_draw_smooth('TRI_STRIP', pts, cols)


def _refboard_stroke(p0, p1, w, rgba):
    """Antialiased thick segment p0->p1: a solid core strip flanked by two
    alpha-falloff strips, delivered as one 8-vertex TRI_STRIP."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    ln = math.hypot(dx, dy)
    if ln < 1e-6:
        return
    nx, ny = -dy / ln, dx / ln
    wi = w * 0.5
    wo = wi + 1.25
    solid = (rgba[0], rgba[1], rgba[2], rgba[3])
    fade = (0.0, 0.0, 0.0, 0.0)
    pts = []
    cols = []
    for off, col in ((-wo, fade), (-wi, solid), (wi, solid), (wo, fade)):
        pts.append((p0[0] + nx * off, p0[1] + ny * off))
        cols.append(col)
        pts.append((p1[0] + nx * off, p1[1] + ny * off))
        cols.append(col)
    _refboard_draw_smooth('TRI_STRIP', pts, cols)


def _refboard_outline(quad, w, rgba):
    """Closed rectangle outline with mitered corners: a solid band plus
    1.25px alpha-falloff rings on both sides, drawn as TRI_STRIPs. Unlike
    four independent strokes, the 90 degree joins line up cleanly."""
    n = len(quad)
    wi = w * 0.5
    # Outward edge normals (quad winds CCW, y-up: outward = right of a->b).
    ns = []
    for i in range(n):
        a, b = quad[i], quad[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy) or 1.0
        ns.append((dy / ln, -dx / ln))
    def ring(off):
        pts = []
        for i in range(n):
            pn, cn = ns[(i - 1) % n], ns[i]
            mx_, my_ = pn[0] + cn[0], pn[1] + cn[1]
            ml = math.hypot(mx_, my_) or 1.0
            mx_, my_ = mx_ / ml, my_ / ml
            d = off / max(0.3, mx_ * cn[0] + my_ * cn[1])
            pts.append((quad[i][0] + mx_ * d, quad[i][1] + my_ * d))
        return pts
    solid = (rgba[0], rgba[1], rgba[2], rgba[3])
    fade = (0.0, 0.0, 0.0, 0.0)
    for ro, co, ri, ci in ((wi + 1.25, fade, wi, solid),
                           (wi, solid, -wi, solid),
                           (-wi, solid, -(wi + 1.25), fade)):
        outer, inner = ring(ro), ring(ri)
        pts, cols = [], []
        for i in range(n + 1):
            j = i % n
            pts.append(outer[j])
            cols.append(co)
            pts.append(inner[j])
            cols.append(ci)
        _refboard_draw_smooth('TRI_STRIP', pts, cols)


def _refboard_mark_color(rgba):
    """Handle-tick color: the outline hue pushed brighter and more
    saturated so the marks pop against the border."""
    import colorsys
    h, s, v = colorsys.rgb_to_hsv(rgba[0], rgba[1], rgba[2])
    r, g, b = colorsys.hsv_to_rgb(
        h, min(1.0, s * 1.35 + 0.05), min(1.0, v * 1.15 + 0.05))
    return (r, g, b, 1.0)


def _refboard_lmark(p0, pv, p1, w, rgba):
    """Antialiased mitered L stroke through p0 -> pv -> p1: a solid core
    band flanked by alpha-falloff edges, with a sharp mitered join at pv
    (no gap) and butt caps at both ends."""
    d1x, d1y = pv[0] - p0[0], pv[1] - p0[1]
    l1 = math.hypot(d1x, d1y) or 1.0
    d1x, d1y = d1x / l1, d1y / l1
    d2x, d2y = p1[0] - pv[0], p1[1] - pv[1]
    l2 = math.hypot(d2x, d2y) or 1.0
    d2x, d2y = d2x / l2, d2y / l2
    n1x, n1y = -d1y, d1x
    n2x, n2y = -d2y, d2x
    mx_, my_ = n1x + n2x, n1y + n2y
    ml = math.hypot(mx_, my_)
    if ml < 1e-4:
        mx_, my_, ml = n1x, n1y, 1.0
    mx_, my_ = mx_ / ml, my_ / ml
    nproj = max(0.35, mx_ * n1x + my_ * n1y)
    wi = w * 0.5
    wo = wi + 1.25

    def ring(o):
        om = o / nproj
        return ((p0[0] + n1x * o, p0[1] + n1y * o),
                (pv[0] + mx_ * om, pv[1] + my_ * om),
                (p1[0] + n2x * o, p1[1] + n2y * o))

    solid = (rgba[0], rgba[1], rgba[2], rgba[3])
    fade = (0.0, 0.0, 0.0, 0.0)
    for ro, co, ri, ci in ((wo, fade, wi, solid),
                           (wi, solid, -wi, solid),
                           (-wi, solid, -wo, fade)):
        a, b = ring(ro), ring(ri)
        pts, cols = [], []
        for i in range(3):
            pts.append(a[i])
            cols.append(co)
            pts.append(b[i])
            cols.append(ci)
        _refboard_draw_smooth('TRI_STRIP', pts, cols)


def _refboard_handle_marks(rect, rgba, hmark=None):
    """Handle marks: L-shaped brackets hugging the outside of each corner
    (crop-mark style) and edge-parallel dashes at midpoints. hmark is
    ('corner', i) or ('edge', i) to highlight the hovered handle - lifted
    color (same as the crop-edge hover) and +1px on every dimension."""
    corners = _refboard_corners_local(rect)
    q = [_refboard_to_screen(rect, *c) for c in corners]
    px, py = rect[4], rect[5]
    hc = tuple(min(1.0, c + (1.0 - c) * 0.6) for c in rgba[:3]) + (1.0,)
    for i in range(4):
        cx, cy = q[i]
        # Unit vectors along each edge out of this corner.
        u = []
        for j in ((i + 1) % 4, (i - 1) % 4):
            ex, ey = q[j][0] - cx, q[j][1] - cy
            el = math.hypot(ex, ey) or 1.0
            u.append((ex / el, ey / el))
        dx, dy = cx - px, cy - py
        dl = math.hypot(dx, dy) or 1.0
        # L vertex sits just outside the corner on its diagonal; the arms
        # run INTO the rect parallel to the edges, so the bracket opens
        # toward the image like a transform handle.
        hot = hmark == ('corner', i)
        vx, vy = cx + dx / dl * 2.5, cy + dy / dl * 2.5
        arm = 15.0 if hot else 14.0
        th = 6.2 if hot else 5.2
        _refboard_lmark((vx + u[0][0] * arm, vy + u[0][1] * arm),
                       (vx, vy),
                       (vx + u[1][0] * arm, vy + u[1][1] * arm),
                       th, hc if hot else rgba)
    for ei, (a_i, b_i) in enumerate(((0, 1), (1, 2), (2, 3), (3, 0))):
        ax_, ay_ = q[a_i]
        bx_, by_ = q[b_i]
        ex, ey = bx_ - ax_, by_ - ay_
        el = math.hypot(ex, ey) or 1.0
        mx_, my_ = (ax_ + bx_) * 0.5, (ay_ + by_) * 0.5
        hot = hmark == ('edge', ei)
        half = 15.0 if hot else 14.0
        _refboard_stroke(
            (mx_ - ex / el * half, my_ - ey / el * half),
            (mx_ + ex / el * half, my_ + ey / el * half),
            6.2 if hot else 5.2, hc if hot else rgba)


def _refboard_sel_color(context):
    try:
        c = context.preferences.themes[0].view_3d.object_selected
        return (c.r, c.g, c.b, 1.0)
    except Exception:
        return (1.0, 0.6, 0.1, 1.0)


# --- rotate hint --------------------------------------------------------------

def _refboard_corner_arc(cx, cy, rot, rgba):
    """180-degree arc hugging a corner from outside, centered on the corner
    itself and spanning the outward diagonal +/- 90 degrees. Drawn as one
    continuous annulus (solid band flanked by alpha-falloff strips) so there
    are no segment joins or caps to catch dirt. Butt ends. Pinned to the
    corner, never follows the mouse."""
    r = _REFBOARD_ROT_R
    w = 4.4
    wi, wo = r - w * 0.5, r + w * 0.5
    a0 = rot - math.pi * 0.5
    seg = 40
    solid = (rgba[0], rgba[1], rgba[2], rgba[3])
    fade = (0.0, 0.0, 0.0, 0.0)
    for r0, c0, r1, c1 in ((wo + 1.25, fade, wo, solid),
                           (wo, solid, wi, solid),
                           (wi, solid, wi - 1.25, fade)):
        pts = []
        cols = []
        for i in range(seg + 1):
            a = a0 + math.pi * i / seg
            ca, sa = math.cos(a), math.sin(a)
            pts.append((cx + r0 * ca, cy + r0 * sa))
            cols.append(c0)
            pts.append((cx + r1 * ca, cy + r1 * sa))
            cols.append(c1)
        _refboard_draw_smooth('TRI_STRIP', pts, cols)


def _refboard_draw_wanted(scene, items):
    """Whether the draw callback has anything to do this frame.

    An empty board is not automatically 'nothing': entering edit mode must
    show the veil even before the first paste, so the raw canvas flag alone
    keeps us drawing."""
    if items:
        return True
    if _refboard_pending:
        return True
    if _refboard_mode_label and \
            (time.time() - _refboard_mode_ts) < 1.5:
        return True
    return _refboard_canvas_on


def _draw_refboard():
    context = bpy.context
    scene = getattr(context, "scene", None)
    region = getattr(context, "region", None)
    if scene is None or region is None or gpu is None or \
            batch_for_shader is None:
        return
    items = getattr(scene, "refboard_items", None)
    if getattr(scene, "refboard_all_hidden", False):
        items = None
    show_help = _refboard_pref("show_help", True)
    if not _refboard_draw_wanted(scene, items):
        return
    tex_shader, flat = _refboard_shaders()
    if flat is None:
        return

    sel = getattr(scene, "refboard_selected", -1)
    drag = _refboard_drag
    sel_col = _refboard_sel_color(context)
    if items:
        _refboard_ensure_ref_size(scene, region, stable=True)
    view = _refboard_view(scene, region)
    # Selection affordances only exist inside canvas mode.
    canvas_on = _refboard_canvas_mode(scene)

    gpu.state.blend_set('ALPHA_PREMULT')
    try:
        # Canvas mode indicator: a 50% grey veil over the 3D view while a
        # live ref is selected - makes "not in 3D view mode" obvious. Drawn
        # under the refs themselves.
        # Canvas mode shows the veil at full strength. While the veil prefs
        # are being dragged the timestamp refreshes and it stays at full
        # alpha for a live preview, fading out 1.5s after the last change.
        # Raw flag, not canvas_mode(): the veil is the edit-mode indicator
        # and must show even with zero refs pasted.
        veil_fade = 1.0 if _refboard_canvas_on else max(
            0.0, 1.0 - (time.time() - _refboard_veil_ts) / 1.5)
        if veil_fade > 0.0:
            dw, dh = float(region.width), float(region.height)
            vc = _refboard_pref("veil_color", (0.0, 0.0, 0.0))
            va = _refboard_pref("veil_alpha", 0.5) * veil_fade
            _refboard_draw_flat(
                flat, 'TRI_FAN',
                [(0.0, 0.0), (dw, 0.0), (dw, dh), (0.0, dh)],
                (vc[0] * va, vc[1] * va, vc[2] * va, va))
            if veil_fade < 1.0:
                try:
                    region.tag_redraw()  # keep the fade animating
                except Exception:
                    pass
        for i in range(len(items) if items is not None else 0):
            item = items[i]
            img = item.image
            if img is None or not item.visible or tex_shader is None:
                continue
            rect = _refboard_rect(item, region, img, view)
            if rect is None:
                continue
            is_sel = canvas_on and (
                (i == sel) or (drag is not None and
                               drag.get("index") == i))
            quad = _refboard_crop_quad(item, region, img, view)
            uvs = _refboard_flip_uvs(item, _refboard_crop_uvs(item))
            l, b, r, t = item.crop
            try:
                tex = gpu.texture.from_image(img)
            except Exception:
                continue
            # While a crop edge is being resized, ghost the full uncropped
            # extent at low opacity so the cropped-away chunk is visible
            # as the bound moves. Not shown for a plain selection.
            if drag is not None and drag.get("index") == i and \
                    drag["mode"] == 'crop' and \
                    (l > 0.0 or b > 0.0 or r < 1.0 or t < 1.0):
                zx = view[0]
                frect = (0.0, 0.0,
                         img.size[0] * item.scale[0] * zx * 0.5,
                         img.size[1] * item.scale[1] * zx * 0.5,
                         rect[4], rect[5], item.rotation)
                try:
                    fbatch = batch_for_shader(
                        tex_shader, 'TRI_FAN',
                        {"pos": _refboard_quad(frect),
                         "texCoord": _refboard_flip_uvs(
                             item, [(0.0, 0.0), (1.0, 0.0),
                                    (1.0, 1.0), (0.0, 1.0)])})
                    tex_shader.bind()
                    if _REFBOARD_TEX_SHADER_NEEDS_MVP:
                        tex_shader.uniform_float(
                            "ModelViewProjectionMatrix", _refboard_mvp())
                        tex_shader.uniform_float(
                            "opacity", item.opacity * 0.15)
                    tex_shader.uniform_sampler("image", tex)
                    fbatch.draw(tex_shader)
                except Exception:
                    pass
            try:
                batch = batch_for_shader(
                    tex_shader, 'TRI_FAN',
                    {"pos": quad, "texCoord": uvs})
                tex_shader.bind()
                if _REFBOARD_TEX_SHADER_NEEDS_MVP:
                    tex_shader.uniform_float(
                        "ModelViewProjectionMatrix", _refboard_mvp())
                    tex_shader.uniform_float("opacity", item.opacity)
                tex_shader.uniform_sampler("image", tex)
                batch.draw(tex_shader)
            except Exception:
                continue

            if not is_sel:
                # Marquee members get a thin dim outline so the group reads.
                if canvas_on and i in _refboard_group:
                    mc = (sel_col[0], sel_col[1], sel_col[2],
                          sel_col[3] * 0.55)
                    _refboard_outline(quad, 1.0, mc)
                continue
            # Crop-zone hover: thicken the hovered edge and hide the scale
            # dots. Quad edge order maps to crop sides as (1,2,3,0).
            # Corner/edge hover: highlight that scale marker instead.
            crop_edge = None
            hmark = None
            if _refboard_hover is not None:
                try:
                    hptr, hzone, hsub = _refboard_hover
                    if hptr == region.as_pointer():
                        if hzone == 'crop':
                            crop_edge = hsub
                        elif hzone in ('corner', 'edge'):
                            hmark = (hzone, hsub)
                except Exception:
                    pass
            # Mitered closed outline so the 90 degree corners line up.
            _refboard_outline(quad, 2.0, sel_col)
            # Hovered crop edge: brighter, thicker overlay, extended a touch
            # past the corners so its join with the outline stays square.
            if crop_edge is not None:
                a_i, b_i = ((0, 1), (1, 2), (2, 3),
                            (3, 0))[(3, 0, 1, 2)[crop_edge]]
                bc = tuple(min(1.0, c + (1.0 - c) * 0.6)
                           for c in sel_col[:3]) + (1.0,)
                ax_, ay_ = quad[a_i]
                bx_, by_ = quad[b_i]
                ex, ey = bx_ - ax_, by_ - ay_
                el = math.hypot(ex, ey) or 1.0
                ext = 2.25
                _refboard_stroke(
                    (ax_ - ex / el * ext, ay_ - ey / el * ext),
                    (bx_ + ex / el * ext, by_ + ey / el * ext),
                    4.5, bc)
            # Handle ticks only for a persistent selection, not a temp
            # drag, and not while the crop affordance owns the edge.
            if i == sel and crop_edge is None and not _refboard_mod_ctrl:
                _refboard_handle_marks(
                    rect, _refboard_mark_color(sel_col), hmark)
        # Marquee group affordance: shared bbox with corner/edge dots, same
        # visual language as a single selected ref.
        if canvas_on and _refboard_group and items is not None:
            grect = _refboard_group_rect_screen(scene, region)
            if grect is not None:
                ghmark = None
                if _refboard_hover is not None:
                    try:
                        hptr, hzone, hsub = _refboard_hover
                        if hptr == region.as_pointer():
                            # Drag zones carry the group_ prefix.
                            z = hzone[6:] if hzone.startswith('group_') \
                                else hzone
                            if z in ('corner', 'edge'):
                                ghmark = (z, hsub)
                    except Exception:
                        pass
                gquad = _refboard_quad(grect)
                _refboard_outline(gquad, 2.0, sel_col)
                if not _refboard_mod_ctrl:
                    _refboard_handle_marks(
                        grect, _refboard_mark_color(sel_col), ghmark)
        # Marquee rect while a box-select or crop drag is live.
        live_mq = _refboard_marquee if _refboard_marquee is not None \
            else _refboard_cropmarq
        if live_mq is not None and \
                live_mq.get("ptr") == region.as_pointer():
            x0, y0 = live_mq["m0"]
            x1, y1 = live_mq["cur"]
            mquad = [(min(x0, x1), min(y0, y1)),
                     (max(x0, x1), min(y0, y1)),
                     (max(x0, x1), max(y0, y1)),
                     (min(x0, x1), max(y0, y1))]
            # An illegal crop drag (outside start overlapping a rotated
            # ref) draws the same translucent rect in red instead of the
            # selection colour - same 0.05 fill, same 1px outline.
            mq_col = (0.95, 0.2, 0.12, sel_col[3]) \
                if live_mq.get("illegal") else sel_col
            _refboard_draw_flat(
                flat, 'TRI_FAN', mquad,
                (mq_col[0] * 0.05, mq_col[1] * 0.05,
                 mq_col[2] * 0.05, 0.05))
            _refboard_outline(mquad, 1.0, mq_col)
    finally:
        gpu.state.blend_set('NONE')
        gpu.state.line_width_set(1.0)

    # Corner arc while the cursor sits in a rotate zone (or a rotate drag is
    # live): wraps the outside of that corner, pinned to it, only in the
    # hovered viewport.
    if _refboard_hover is not None and items is not None and canvas_on:
        try:
            hptr, hzone, corner_i = _refboard_hover
            hidx = _refboard_drag["index"] if _refboard_drag else sel
            rect = None
            if hzone == 'rotate' and \
                    hptr == region.as_pointer():
                if _refboard_group:
                    rect = _refboard_group_rect_screen(scene, region)
                elif 0 <= hidx < len(items):
                    item = items[hidx]
                    rect = _refboard_rect(item, region, item.image, view) \
                        if item.image is not None else None
                if rect is not None:
                    quad = _refboard_quad(rect)
                    cxp, cyp = quad[corner_i]
                    dx, dy = cxp - rect[4], cyp - rect[5]
                    dl = math.hypot(dx, dy)
                    if dl > 1e-4:
                        gpu.state.blend_set('ALPHA_PREMULT')
                        _refboard_corner_arc(
                            cxp, cyp, math.atan2(dy / dl, dx / dl), sel_col)
                        gpu.state.blend_set('NONE')
        except Exception:
            pass

    # Opacity % readout just under the image's screen bbox while an opacity
    # drag is live and for a second after release. Axis-aligned, unrotated.
    if blf is not None and _refboard_opacity_label is not None and \
            items is not None:
        try:
            lptr, lidx, until = _refboard_opacity_label
            if lptr == region.as_pointer() and until > time.time():
                rect = None
                op = 1.0
                if lidx == -1:
                    if _refboard_group:
                        rect = _refboard_group_rect_screen(scene, region)
                        idxs = _refboard_group
                    else:
                        # Board-wide opacity drag: the readout sits under
                        # the union bbox of every ref and shows the mean.
                        idxs = list(range(len(items)))
                        xs, ys = [], []
                        for j in idxs:
                            it = items[j]
                            if it.image is None:
                                continue
                            r_ = _refboard_rect(it, region, it.image, view)
                            if r_ is not None:
                                q_ = _refboard_quad(r_)
                                xs += [q[0] for q in q_]
                                ys += [q[1] for q in q_]
                        if xs:
                            rect = (0.0, 0.0,
                                    (max(xs) - min(xs)) * 0.5,
                                    (max(ys) - min(ys)) * 0.5,
                                    (min(xs) + max(xs)) * 0.5,
                                    (min(ys) + max(ys)) * 0.5, 0.0)
                    ops = [items[j].opacity for j in idxs
                           if j < len(items)]
                    op = sum(ops) / len(ops) if ops else 1.0
                elif 0 <= lidx < len(items):
                    item = items[lidx]
                    img = item.image
                    rect = _refboard_rect(item, region, img, view) \
                        if img is not None else None
                    op = item.opacity
                if rect is not None:
                    quad = _refboard_quad(rect)
                    xs = [q[0] for q in quad]
                    ys = [q[1] for q in quad]
                    text = f"{round(op * 100)}%"
                    cx = (min(xs) + max(xs)) * 0.5
                    blf.size(0, 13.0)
                    tw, th = blf.dimensions(0, text)
                    blf.position(0, cx - tw * 0.5,
                                 min(ys) - th - 14.0, 0)
                    blf.color(0, 1.0, 1.0, 1.0, 1.0)
                    blf.draw(0, text)
                    if lidx == -1 and not _refboard_group:
                        # Board-wide drag: caption the readout so it isn't
                        # a bare floating number.
                        blf.size(0, 13.0)
                        lw = blf.dimensions(0, "Global Opacity")[0]
                        blf.position(0, cx - lw * 0.5,
                                     min(ys) - 10.0, 0)
                        blf.color(0, 1.0, 1.0, 1.0, 0.75)
                        blf.draw(0, "Global Opacity")
        except Exception:
            pass

    # Progress strip under the point the image will paste at (screen-space
    # pastes), or bottom-center for 3D pastes.
    if _refboard_pending:
        bw, bh = 140.0, 4.0
        p0 = _refboard_pending[0]
        st = p0.get("state") or {}
        if p0.get("mode") == 'SCREEN':
            pos = st.get("pos") or (0.5, 0.5)
            pv = _refboard_view(scene, region)
            pw0, ph0 = _refboard_view_dims(pv, region)
            bx = min(max(8.0,
                         ((pos[0] - 0.5) * pv[0] + 0.5 +
                          pv[1][0]) * pw0 - bw * 0.5),
                     region.width - bw - 8.0)
            by = max(8.0, ((pos[1] - 0.5) * pv[0] + 0.5 +
                           pv[1][1]) * ph0 - 44.0)
        else:
            bx = region.width * 0.5 - bw * 0.5
            by = 34.0
        # The clipboard subprocess gives no real percentage, so the bar
        # chases a moving target: it eases toward ~92% while the proc runs,
        # then sweeps the rest of the way once the proc is done - per-frame
        # exponential chase keeps motion smooth at any redraw rate and the
        # bar always visibly reaches full before the ref lands.
        now = time.time()
        elapsed = max(0.0, now - p0.get("t0", now))
        target = 1.0 if p0.get("done") else \
            min(0.92, 1.0 - math.exp(-elapsed / 0.8))
        shown = p0.get("shown", 0.0)
        dt = min(0.25, max(0.0, now - p0.get("shown_t", now)))
        frac = shown + (target - shown) * min(1.0, dt * 10.0)
        p0["shown"] = frac
        p0["shown_t"] = now
        fill = bw * frac
        gpu.state.blend_set('ALPHA_PREMULT')
        _refboard_draw_flat(flat, 'TRI_FAN',
                           [(bx, by), (bx + bw, by),
                            (bx + bw, by + bh), (bx, by + bh)],
                           (0.07, 0.07, 0.07, 0.7))
        _refboard_draw_flat(flat, 'TRI_FAN',
                           [(bx, by), (bx + fill, by),
                            (bx + fill, by + bh), (bx, by + bh)],
                           (0.315, 0.585, 0.9, 0.9))
        gpu.state.blend_set('NONE')
        if blf is not None:
            try:
                blf.position(0, bx + bw * 0.5 - 42.0, by + bh + 6.0, 0)
                blf.size(0, 11.0)
                blf.color(0, 0.85, 0.85, 0.85, 0.9)
                blf.draw(0, "Pasting image...")
            except Exception:
                pass

    # Help overlay, bottom-right: persistent for the whole edit-mode
    # session; only H (the Show Help pref) toggles it.
    if show_help and blf is not None and region.type == 'WINDOW' and \
            _refboard_canvas_on:
        try:
            _refboard_draw_help(
                region, 1.0, _refboard_pref("help_size", 16.0))
        except Exception:
            pass

    # Edit mode gets a persistent caption, not a timed flash - it is the
    # mode indicator for as long as the mode is on. Exit/hide still flash
    # their glyph for 1.5s. Falls back to text when no icon can be drawn.
    if _refboard_canvas_on and region.type == 'WINDOW':
        try:
            _refboard_draw_edit_caption(region)
        except Exception:
            pass
    elif region.type == 'WINDOW' and _refboard_mode_flash_visible(scene):
        mfade = max(0.0, 1.0 - (time.time() - _refboard_mode_ts) / 1.5)
        if mfade > 0.0:
            drew = _refboard_draw_mode_icons(
                region, tex_shader, _refboard_mode_label, mfade)
            if not drew and blf is not None:
                try:
                    size = 18.0
                    blf.size(0, size)
                    tw, th = blf.dimensions(0, _refboard_mode_label)
                    try:
                        blf.enable(0, blf.SHADOW)
                        blf.shadow(0, 3, 0.0, 0.0, 0.0, 0.8 * mfade)
                    except Exception:
                        pass
                    blf.color(0, 0.95, 0.95, 0.95, 0.95 * mfade)
                    blf.position(
                        0, region.width * 0.5 - tw * 0.5, 20.0, 0)
                    blf.draw(0, _refboard_mode_label)
                    blf.disable(0, blf.SHADOW)
                except Exception:
                    pass
            if mfade < 1.0:
                try:
                    region.tag_redraw()
                except Exception:
                    pass


def _refboard_mode_flash_visible(scene):
    """Whether the mode glyph should flash at all.

    Suppressed on an empty board: announcing a mode is meaningless before any
    reference has been pasted. Reads the collection directly, because the
    caller's `items` is blanked while the board is hidden - and "hidden" is
    exactly one of the modes that wants to flash.
    """
    if not _refboard_mode_label:
        return False
    if len(getattr(scene, "refboard_items", None) or []):
        return True
    # Edit mode announces itself even before the first paste - the veil is
    # up and the label is how the paste-into-edit-mode flow is discovered.
    return _refboard_mode_label == "Refboard Edit"


_refboard_icon_cache = {}

# Mode-flash glyphs: (mode label, icon file). Only the active one is drawn.
# "Refboard Exit" means the board is visible but not editable, which is the
# state both leaving edit mode and un-hiding the board land in.
_REFBOARD_MODE_ICONS = (
    ("Refboard Exit", "visible.png"),
    ("Refboard Off", "invisible.png"),
)

# Peak opacity of the mode glyph, before the flash fade is applied.
_REFBOARD_MODE_ICON_ALPHA = 0.5

# Drawn width of the mode glyph in pixels: 200% of the original 30px row.
# Integer, because the glyph is box-filtered to exactly this size and then
# drawn one texel per pixel.
_REFBOARD_MODE_ICON_SIZE = 60


def _refboard_box_resample(px, w, h, tw, th):
    """Area-average `px` (flat RGBA floats) from w x h down to tw x th.

    GPUTexture exposes no sampler state to Python, so a texture bound to the
    shader is point-sampled: a 256px glyph drawn at 60px would drop most of
    its texels and alias badly. Averaging every source pixel that falls in a
    destination cell is the correct downscale filter, and the result is drawn
    texel-to-pixel so the GPU never resamples it.

    The glyphs are premultiplied (alpha tracks luminance), and averaging is
    valid in premultiplied space, so no un/re-multiply round trip is needed.
    """
    out = [0.0] * (tw * th * 4)
    for ty in range(th):
        sy0 = int(ty * h / th)
        sy1 = max(sy0 + 1, int((ty + 1) * h / th))
        for tx in range(tw):
            sx0 = int(tx * w / tw)
            sx1 = max(sx0 + 1, int((tx + 1) * w / tw))
            r = g = b = a = 0.0
            cnt = 0
            for sy in range(sy0, sy1):
                row = sy * w
                for sx in range(sx0, sx1):
                    i = (row + sx) * 4
                    r += px[i]
                    g += px[i + 1]
                    b += px[i + 2]
                    a += px[i + 3]
                    cnt += 1
            o = (ty * tw + tx) * 4
            inv = 1.0 / cnt
            out[o] = r * inv
            out[o + 1] = g * inv
            out[o + 2] = b * inv
            out[o + 3] = a * inv
    return out


def _refboard_icon(fname, target_w=None):
    """Lazy-load an addon glyph as a GPU texture, cached as (tex, w, h).

    Textures draw under ALPHA_PREMULT, so pixels need real alpha: if the PNG
    is opaque (white-on-black), its luminance becomes the alpha so the glyph
    still composites cleanly.

    `target_w` downsamples the glyph to its drawn width with a box filter,
    which is where the antialiasing comes from - see _refboard_box_resample.

    The pixels are uploaded to the GPU and the Image datablock is dropped
    immediately. Keeping it would leave a *modified* image in bpy.data, which
    makes Blender ask "Save modified images?" on quit and writes the addon's
    own UI glyphs into the user's .blend.
    """
    key = (fname, int(target_w) if target_w else 0)
    entry = _refboard_icon_cache.get(key, False)
    if entry is not False:
        return entry
    entry = None
    img = None
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "img", fname)
        if os.path.isfile(p) and gpu is not None:
            img = bpy.data.images.load(p, check_existing=False)
            w, h = img.size[0], img.size[1]
            n = w * h
            if n > 0:
                px = [0.0] * (n * 4)
                img.pixels.foreach_get(px)
                if min(px[3::4]) >= 0.999:
                    # No real alpha: use luminance as coverage.
                    for i in range(n):
                        px[i * 4 + 3] = max(px[i * 4], px[i * 4 + 1],
                                            px[i * 4 + 2])
                if target_w and 0 < int(target_w) < w:
                    tw = int(target_w)
                    th = max(1, int(round(tw * h / w)))
                    px = _refboard_box_resample(px, w, h, tw, th)
                    w, h = tw, th
                buf = gpu.types.Buffer('FLOAT', w * h * 4, px)
                tex = gpu.types.GPUTexture((w, h), format='RGBA16F',
                                           data=buf)
                entry = (tex, w, h)
    except Exception:
        entry = None
    finally:
        # Drop the datablock either way: nothing dirty, nothing saved.
        if img is not None:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass
    _refboard_icon_cache[key] = entry
    return entry


def _refboard_draw_edit_caption(region):
    """Persistent edit-mode caption: 'Refboard (Edit Mode)' bottom-center,
    shown for as long as the mode is on - not a timed flash."""
    if blf is None:
        return
    try:
        text = "Refboard (Edit Mode)"
        size = 18.0
        blf.size(0, size)
        tw, th = blf.dimensions(0, text)
        try:
            blf.enable(0, blf.SHADOW)
            blf.shadow(0, 3, 0.0, 0.0, 0.0, 0.8)
        except Exception:
            pass
        blf.color(0, 0.95, 0.95, 0.95, 0.9)
        blf.position(0, region.width * 0.5 - tw * 0.5, 34.0, 0)
        blf.draw(0, text)
        blf.disable(0, blf.SHADOW)
    except Exception:
        try:
            blf.disable(0, blf.SHADOW)
        except Exception:
            pass


def _refboard_draw_mode_icons(region, tex_shader, label, alpha):
    """Flash the glyph for the mode that was just selected, bottom-center.
    Only the active mode is drawn - the other two would just be noise.
    Returns False if nothing could be drawn."""
    if tex_shader is None or gpu is None or \
            not _REFBOARD_TEX_SHADER_NEEDS_MVP:
        return False
    fname = next((f for lbl, f in _REFBOARD_MODE_ICONS if lbl == label), None)
    if fname is None:
        return False
    size = _REFBOARD_MODE_ICON_SIZE
    entry = _refboard_icon(fname, target_w=size)
    if not entry:
        return False
    tex, iw, ih = entry
    if tex is None or iw <= 0 or ih <= 0:
        return False
    y = 34.0      # sitting a little higher off the bottom edge
    # `alpha` still carries the flash fade-out ramp.
    alpha *= _REFBOARD_MODE_ICON_ALPHA
    # Draw at the texture's own size: one texel per pixel, so the box-filtered
    # glyph is not resampled again by the point-sampling shader.
    w = float(iw)
    h = float(ih)
    x = round(region.width * 0.5 - w * 0.5)
    uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    drew = False
    try:
        gpu.state.blend_set('ALPHA_PREMULT')
        quad = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        batch = batch_for_shader(
            tex_shader, 'TRI_FAN', {"pos": quad, "texCoord": uvs})
        tex_shader.bind()
        tex_shader.uniform_float("ModelViewProjectionMatrix",
                                 _refboard_mvp())
        tex_shader.uniform_float("opacity", alpha)
        tex_shader.uniform_sampler("image", tex)
        batch.draw(tex_shader)
        drew = True
        gpu.state.blend_set('NONE')
    except Exception:
        try:
            gpu.state.blend_set('NONE')
        except Exception:
            pass
    return drew


def _refboard_veil_update(self, context):
    # Refresh the preview window on every tick so the veil stays up while
    # the sliders are being dragged, then fades out 1.5s after release.
    global _refboard_veil_ts
    _refboard_veil_ts = time.time()
    _refboard_redraw_views()


def _refboard_help_size_update(self, context):
    # Redraw so a size change shows immediately in the persistent help.
    global _refboard_help_ts
    _refboard_help_ts = time.time()
    _refboard_redraw_views()


_REFBOARD_HELP_HEADER = "Refboard Help (H)"

_REFBOARD_HELP_LINES = (
    ("Move", "LMB-drag image"),
    ("Rotate", "Drag arc past a corner"),
    ("Scale", "Drag frame handle or CTRL+ALT drag"),
    ("Crop", "CTRL+drag a rect"),
    ("Flip", "CTRL+SHIFT quick drag"),
    ("Group", "LMB-drag empty space"),
    ("Depth", "[ back or ] front"),
    ("Mode", "` board or ALT+` edit"),
    ("Menu", "RMB options"),
    ("Opacity", "CTRL+RMB drag"),
    ("Copy/Paste", "CTRL+C or CTRL+V"),
    ("Undo/Redo", "CTRL+Z or CTRL+SHIFT+Z"),
    ("Pan", "MMB or ALT+MMB drag"),
    ("Zoom", "Wheel or ALT+RMB drag"),
)


def _refboard_draw_help(region, fade, size=11.0):
    """Bottom-right help block. Function name left, key/drag combo indented
    in a right column. fade (0..1) scales all alphas for the ease-out."""
    lines = _REFBOARD_HELP_LINES
    size = max(4.0, float(size))
    blf.size(0, size)
    header = _REFBOARD_HELP_HEADER
    col_w = max(blf.dimensions(0, k)[0] for k, _ in lines) + size * 1.5
    maxw = max(col_w + max(blf.dimensions(0, v)[0] for _, v in lines),
               blf.dimensions(0, header)[0])
    lh = size * 1.45
    x0 = region.width - 18.0 - maxw
    y0 = 16.0
    try:
        blf.enable(0, blf.SHADOW)
        blf.shadow(0, 3, 0.0, 0.0, 0.0, 0.8 * fade)
    except Exception:
        pass
    for i, (k, v) in enumerate(lines):
        yy = y0 + (len(lines) - 1 - i) * lh
        blf.color(0, 0.95, 0.95, 0.95, 0.9 * fade)
        blf.position(0, x0, yy, 0)
        blf.draw(0, k)
        blf.color(0, 0.72, 0.72, 0.72, 0.85 * fade)
        blf.position(0, x0 + col_w, yy, 0)
        blf.draw(0, v)
    # Header caps the block, one line gap above the top row.
    blf.color(0, 0.95, 0.95, 0.95, 0.95 * fade)
    blf.position(0, x0, y0 + len(lines) * lh + lh * 0.6, 0)
    blf.draw(0, header)
    try:
        blf.disable(0, blf.SHADOW)
    except Exception:
        pass


# --- async paste --------------------------------------------------------------

def _refboard_redraw_views():
    try:
        for w in bpy.context.window_manager.windows:
            if w.screen is None:
                continue
            for a in w.screen.areas:
                if a.type == 'VIEW_3D':
                    a.tag_redraw()
    except Exception:
        pass


def _refboard_flag_update(item, context):
    """Visibility/lock toggles from the panel: hiding or locking releases the
    selection so an inert image can't be edited or deleted blind."""
    scene = getattr(context, "scene", None)
    items = getattr(scene, "refboard_items", None) if scene else None
    if items is not None and (not item.visible or item.locked):
        for i, it in enumerate(items):
            if it == item and scene.refboard_selected == i:
                scene.refboard_selected = -1
    _refboard_redraw_views()


def _refboard_finish(p):
    global _refboard_canvas_on, _refboard_mode_ts, _refboard_mode_label, \
        _refboard_help_ts
    dst = p["dst"]
    st = p.get("state") or {}
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        return
    img = _refboard_adopt(dst)
    # size access forces the lazy decode: a garbage payload keeps (0, 0).
    bad = img.size[0] <= 0 or img.size[1] <= 0
    icon = (not bad and st.get("clipboard") and
            max(img.size[0], img.size[1]) <= 64)
    if bad or icon:
        # Undecodable payloads would draw as a flat magenta quad; icon-size
        # clipboard bitmaps (file/OLE icon side-channels) pasting as refs
        # are never what the user meant. Scrap the datablock either way.
        try:
            bpy.data.images.remove(img)
        except Exception:
            pass
        raise RuntimeError(
            "clipboard image could not be decoded" if bad else
            "clipboard image looks like an icon (<=64px)")
    if not st.get("filepath") and not img.name.startswith("Refboard"):
        img.name = "Refboard"
    if p["mode"] == 'SCREEN':
        scene = st.get("scene") or bpy.context.scene
        _refboard_undo_seed(scene)
        item = scene.refboard_items.add()
        item.image = img
        taken = {it.name for it in scene.refboard_items if it != item}
        n = 1
        while "img.%03d" % n in taken:
            n += 1
        item.name = "img.%03d" % n
        # pos is a canvas fraction already (converted at invoke), so the
        # image lands under the cursor at whatever pan/zoom was current.
        pos = st.get("pos") or (0.5, 0.5)
        item.pos = (pos[0], pos[1])
        # Pin the canvas to the viewport this board was authored in.
        _refboard_ensure_ref_size(
            scene, _RefboardRegion(float(st.get("rw") or 0),
                                   float(st.get("rh") or 0)))
        # Fit to ~40% of the canvas it was pasted over, capped at native
        # size, expressed in unzoomed canvas units.
        iw, ih = img.size[0], img.size[1]
        if iw > 0 and ih > 0:
            cw0 = max(1.0, float(st.get("cw") or st.get("rw") or 800))
            ch0 = max(1.0, float(st.get("ch") or st.get("rh") or 600))
            zoom0 = max(1e-4, float(
                getattr(scene, "refboard_view_zoom", 1.0)))
            s = min(1.0, (cw0 * 0.4) / iw, (ch0 * 0.4) / ih) / zoom0
            item.scale = (max(0.01, s), max(0.01, s))
        scene.refboard_selected = len(scene.refboard_items) - 1
        # Pasting drops straight into canvas Edit mode with the new ref
        # selected; flash the mode label and pop the help block.
        _refboard_canvas_on = True
        _refboard_enter_object_mode()
        _refboard_npanel_hide()
        scene.refboard_all_hidden = False
        _refboard_mode_ts = _refboard_help_ts = time.time()
        _refboard_mode_label = "Refboard Edit"
        _refboard_ensure_modal()
    else:
        obj = bpy.data.objects.new("Refboard", None)
        obj.empty_display_type = 'IMAGE'
        obj.data = img
        coll = st.get("collection")
        try:
            if coll is not None:
                coll.objects.link(obj)
            else:
                (st.get("scene") or bpy.context.scene
                 ).collection.objects.link(obj)
        except Exception:
            bpy.context.scene.collection.objects.link(obj)
        sc = st.get("scene") or bpy.context.scene
        try:
            obj.location = sc.cursor.location
        except Exception:
            pass
        basis = st.get("basis")
        if basis:
            view_dir, view_right, view_up = basis
            normal = _refboard_cardinal_normal(view_dir)
            _refboard_align_cardinal(obj, view_dir, normal,
                                    view_right, view_up)
    _refboard_undo_push("Refboard Paste")


def _refboard_status(msg, duration=3.0):
    """Native bottom status-bar message.

    WorkSpace.status_text_set writes the same bar that shows key hints /
    "No objects to paste" reports (context.window has no such method - an
    earlier version silently died on that AttributeError). The text
    persists until cleared, so a one-shot timer restores the bar.
    """
    try:
        ws = getattr(bpy.context, "workspace", None)
        if ws is None:
            return
        ws.status_text_set(msg)
        try:
            for win in bpy.context.window_manager.windows:
                for a in win.screen.areas:
                    a.tag_redraw()
        except Exception:
            pass
        def _clear():
            try:
                w = getattr(bpy.context, "workspace", None)
                if w is not None:
                    w.status_text_set(None)
            except Exception:
                pass
            return None
        bpy.app.timers.register(_clear, first_interval=duration)
    except Exception:
        pass


def _refboard_report_no_image():
    """Zero-delay timer: run the paste op outside the modal event so its
    report travels the normal dispatch path (reports raised by an op
    invoked *inside* a modal handler aren't guaranteed a toast)."""
    try:
        bpy.ops.refboard.paste('INVOKE_DEFAULT')
    except Exception as e:
        print("Refboard paste failed:", e)
    return None


def _refboard_poll_timer():
    if not _refboard_pending:
        return None
    now = time.time()
    for p in list(_refboard_pending):
        if not p.get("done"):
            if p["proc"].poll() is None:
                continue
            try:
                p["proc"].communicate(timeout=1)
            except Exception:
                pass
            p["done"] = True
            p["done_t"] = now
        # Hold the entry until the bar has visibly swept to 100% (deadline
        # guards headless/no-redraw contexts so the paste can't stall).
        if p.get("shown", 0.0) < 0.99 and \
                now - p.get("done_t", now) < 0.5:
            continue
        _refboard_pending.remove(p)
        try:
            _refboard_finish(p)
        except Exception as e:
            print("Refboard paste failed:", e)
            _refboard_status("Refboard: %s" % e)
    if not _refboard_pending:
        return None
    # Repaint every tick so the bar moves instead of stepping between the
    # incidental redraws other handlers happen to cause.
    _refboard_redraw_views()
    return 0.05


# --- interaction --------------------------------------------------------------

def _refboard_snapshot(item):
    return (tuple(item.pos), tuple(item.scale),
            item.rotation, tuple(item.crop), item.opacity,
            tuple(item.crop_pts), tuple(item.home_scale),
            (item.flip_x, item.flip_y))


def _refboard_restore(item, snap):
    item.pos, item.scale, item.rotation, item.crop, item.opacity = \
        snap[:5]
    if len(snap) >= 6:
        item.crop_pts = snap[5]
    if len(snap) >= 7:
        item.home_scale = snap[6]
    if len(snap) >= 8:
        item.flip_x, item.flip_y = snap[7]


def _refboard_start_drag(scene, region, idx, zone, sub, mx, my,
                        button='LEFTMOUSE'):
    _refboard_ensure_ref_size(scene, region)
    item = scene.refboard_items[idx]
    img = item.image
    # Drag math runs in canvas space: convert the mouse once so pan/zoom
    # doesn't corrupt deltas mid-drag.
    cw, ch = _refboard_ref_size(scene, region)
    rect = _refboard_rect(item, region, img,
                          _refboard_canvas_view(scene, region))
    cmx, cmy = _refboard_to_canvas_px(region, scene, mx, my)
    lx, ly = _refboard_to_local(rect, cmx, cmy)
    # Pivot for rotate/center-scale drags is the visible crop quad's
    # centroid - the uncropped image center (pos) can sit far outside the
    # crop window, which would swing the cropped result around it.
    cuv = _refboard_crop_uvs(item)
    cu = sum(p[0] for p in cuv) * 0.25 - 0.5
    cv = sum(p[1] for p in cuv) * 0.25 - 0.5
    piw, pih = float(img.size[0]), float(img.size[1])
    pix, piy = cu * piw * item.scale[0], cv * pih * item.scale[1]
    pc, ps = math.cos(item.rotation), math.sin(item.rotation)
    cpiv = (item.pos[0] * cw + pix * pc - piy * ps,
            item.pos[1] * ch + pix * ps + piy * pc)
    # Scale drags must pivot on the handles the user actually sees, which are
    # the corners of the best-fit rect, not the raw crop quad. For a rotated
    # image cropped by a screen marquee the stored quad is a parallelogram in
    # pixel space (UV space is normalized, so a UV rotation shears once the
    # non-square image dimensions are applied) and the two do not coincide.
    # Inverse-transform the drawn corners into image space so the existing
    # pivot/re-anchor math operates on exactly that geometry.
    rect_pts_img = None
    if rect is not None:
        ic, is_ = math.cos(-item.rotation), math.sin(-item.rotation)
        sx0 = item.scale[0] if abs(item.scale[0]) > 1e-9 else 1e-9
        sy0 = item.scale[1] if abs(item.scale[1]) > 1e-9 else 1e-9
        rect_pts_img = []
        for qx, qy in _refboard_quad(rect):
            ddx = qx - item.pos[0] * cw
            ddy = qy - item.pos[1] * ch
            rect_pts_img.append(((ddx * ic - ddy * is_) / sx0,
                                 (ddx * is_ + ddy * ic) / sy0))
    if zone in _REFBOARD_YIELD_MODES:
        _refboard_yield_begin(scene, {idx}, region)
    _refboard_drag_set({
        "rect_pts_img": rect_pts_img,
        "index": idx,
        "mode": zone,
        "sub": sub,
        "temp": zone == 'inside_temp',
        "moved": False,
        "m0": (cmx, cmy),
        "m0_screen": (mx, my),
        "snap": _refboard_snapshot(item),
        "local0": (lx, ly),
        "scale0": tuple(item.scale),
        "rot0": item.rotation,
        "pos0": tuple(item.pos),
        "crop0": tuple(item.crop),
        "crop_uv0": tuple(v for p in cuv for v in p),
        "cpiv": cpiv,
        "cpiv_img": (cu * piw, cv * pih),
        "op0": item.opacity,
        "button": button,
        "angle0": math.atan2(cmy - cpiv[1], cmx - cpiv[0]),
        "dist0": max(1e-4, math.hypot(lx - rect[0], ly - rect[1])),
        "screen_dist0": max(1e-4,
                            math.hypot(cmx - rect[4], cmy - rect[5])),
    })


def _refboard_group_start_drag(scene, region, zone, sub, mx, my,
                              button='LEFTMOUSE'):
    """Begin a collective drag on the marquee group. The shared pivot is the
    group bbox's: opposite corner/edge-midpoint for scale drags (same anchor
    rules as a single ref) and the bbox center for rotate/center-scale."""
    _refboard_ensure_ref_size(scene, region)
    grect = _refboard_group_rect(scene, region)
    if grect is None:
        return
    cmx, cmy = _refboard_to_canvas_px(region, scene, mx, my)
    hx, hy, px, py = grect[2], grect[3], grect[4], grect[5]
    members = []
    for i in _refboard_group:
        if 0 <= i < len(scene.refboard_items):
            it = scene.refboard_items[i]
            members.append((i, tuple(it.pos), tuple(it.scale),
                            it.rotation, tuple(it.crop), it.opacity))
    pivot = (px, py)
    grab = (cmx, cmy)
    if zone == 'corner':
        corners = [(px - hx, py - hy), (px + hx, py - hy),
                   (px + hx, py + hy), (px - hx, py + hy)]
        grab = corners[sub]
        pivot = corners[(sub + 2) % 4]
    elif zone == 'edge':
        # sub: 0 bottom,1 right,2 top,3 left - pivot on the opposite edge.
        pivot = {0: (px, py + hy), 1: (px - hx, py),
                 2: (px, py - hy), 3: (px + hx, py)}[sub]
    if ("group_" + zone) in _REFBOARD_YIELD_MODES:
        _refboard_yield_begin(scene, set(_refboard_group), region)
    _refboard_drag_set({
        "index": -1,
        "mode": "group_" + zone,
        "sub": sub,
        "temp": False,
        "moved": False,
        "m0": (cmx, cmy),
        "m0_screen": (mx, my),
        "button": button,
        "snap": None,
        "group_members": members,
        "grect": grect,
        "pivot": pivot,
        "grab": grab,
        "center": (px, py),
        "angle0": math.atan2(cmy - py, cmx - px),
        "local0": (0.0, 0.0),
        "scale0": (1.0, 1.0),
        "rot0": 0.0,
        "pos0": (0.0, 0.0),
        "crop0": (0.0, 0.0, 1.0, 1.0),
        "op0": 1.0,
        "dist0": 1.0,
        "screen_dist0": 1.0,
    })


def _refboard_drag_set(d):
    global _refboard_drag
    _refboard_drag = d


def _refboard_drag_update(scene, region, mx, my, event=None):
    d = _refboard_drag
    if d is None:
        return
    items = scene.refboard_items
    idx = d["index"]
    if idx >= len(items):
        return
    if d["mode"].startswith('group_'):
        _refboard_group_drag_update(scene, region, d, mx, my, event)
        return
    item = items[idx]
    img = item.image
    if img is None:
        return
    iw, ih = float(img.size[0]), float(img.size[1])
    # Canvas-space rect and mouse keep item math independent of zoom and
    # of the viewport size.
    cw, ch = _refboard_ref_size(scene, region)
    rect = _refboard_rect(item, region, img,
                          _refboard_canvas_view(scene, region))
    cmx, cmy = _refboard_to_canvas_px(region, scene, mx, my)
    lx, ly = _refboard_to_local(rect, cmx, cmy)
    sm0 = d.get("m0_screen", d["m0"])
    if math.hypot(mx - sm0[0], my - sm0[1]) > 3.0:
        d["moved"] = True
    mode = d["mode"]
    if mode in ('inside', 'inside_temp'):
        item.pos = (d["pos0"][0] + (cmx - d["m0"][0]) / cw,
                    d["pos0"][1] + (cmy - d["m0"][1]) / ch)
    elif mode in ('corner', 'edge'):
        # Uniform scale anchored at the opposite corner/edge midpoint.
        # The factor is projected in SCREEN space on the drag-start quad:
        # a rotated crop is a sheared parallelogram in image space, so an
        # image-space edge normal does not match the visible edge's screen
        # normal and top/bottom drags would mis-measure the mouse.
        s0 = d["scale0"]
        rot0 = d["rot0"]
        cu0 = d.get("crop_uv0")
        rpi = d.get("rect_pts_img")
        if rpi is not None:
            # Corners of the on-screen handle rect, in image space.
            pts = [tuple(p) for p in rpi]
        elif cu0 is not None:
            pts = [((cu0[k] - 0.5) * iw, (cu0[k + 1] - 0.5) * ih)
                   for k in (0, 2, 4, 6)]
        else:
            l, b, r, t = d["crop0"]
            pts = [((l - 0.5) * iw, (b - 0.5) * ih),
                   ((r - 0.5) * iw, (b - 0.5) * ih),
                   ((r - 0.5) * iw, (t - 0.5) * ih),
                   ((l - 0.5) * iw, (t - 0.5) * ih)]
        # Screen-space positions of the quad corners at drag start.
        rc, rs = math.cos(rot0), math.sin(rot0)
        qpts = [(d["pos0"][0] * cw +
                 px * s0[0] * rc - py * s0[1] * rs,
                 d["pos0"][1] * ch +
                 px * s0[0] * rs + py * s0[1] * rc)
                for px, py in pts]
        if mode == 'corner':
            # Opposite corner is the pivot; factor is the projection of the
            # mouse onto the anchor->grabbed-corner diagonal.
            sub = d["sub"]
            ax, ay = pts[(sub + 2) % 4]
            asx, asy = qpts[(sub + 2) % 4]
            gsx, gsy = qpts[sub]
            dxv, dyv = gsx - asx, gsy - asy
            den = max(1e-4, dxv * dxv + dyv * dyv)
            f = ((cmx - asx) * dxv + (cmy - asy) * dyv) / den
        else:
            sub = d["sub"]
            # sub: 0 bottom,1 right,2 top,3 left - pivot on the opposite
            # edge midpoint; factor is the projected distance between the
            # edge midlines along the dragged edge's SCREEN normal.
            a1, a2 = pts[(sub + 2) % 4], pts[(sub + 3) % 4]
            ax, ay = (a1[0] + a2[0]) * 0.5, (a1[1] + a2[1]) * 0.5
            a1s, a2s = qpts[(sub + 2) % 4], qpts[(sub + 3) % 4]
            asx = (a1s[0] + a2s[0]) * 0.5
            asy = (a1s[1] + a2s[1]) * 0.5
            g1s, g2s = qpts[sub], qpts[(sub + 1) % 4]
            gsx = (g1s[0] + g2s[0]) * 0.5
            gsy = (g1s[1] + g2s[1]) * 0.5
            nx, ny = g1s[1] - g2s[1], g2s[0] - g1s[0]
            nl = max(1e-4, math.hypot(nx, ny))
            nx, ny = nx / nl, ny / nl
            if (gsx - asx) * nx + (gsy - asy) * ny < 0.0:
                nx, ny = -nx, -ny
            den = max(1e-4, (gsx - asx) * nx + (gsy - asy) * ny)
            f = ((cmx - asx) * nx + (cmy - asy) * ny) / den
        f = max(0.02, f)
        nsx, nsy = s0[0] * f, s0[1] * f
        # Re-center so the pivot texel stays put on screen.
        a_scr_x = d["pos0"][0] * cw + ax * s0[0] * rc - \
            ay * s0[1] * rs
        a_scr_y = d["pos0"][1] * ch + ax * s0[0] * rs + \
            ay * s0[1] * rc
        nx_px = a_scr_x - (ax * nsx * rc - ay * nsy * rs)
        ny_px = a_scr_y - (ax * nsx * rs + ay * nsy * rc)
        item.scale = (max(0.01, nsx), max(0.01, nsy))
        item.pos = (nx_px / cw, ny_px / ch)
    elif mode in ('rotate', 'center_scale'):
        # Pivot is the crop quad's centroid, not the uncropped image
        # center (pos): an off-center crop would otherwise orbit/drift
        # around the latent center. cpiv/cpiv_img are captured at drag
        # start; fall back to the crop0 rect centroid for hand-built
        # drag dicts.
        cpiv = d.get("cpiv")
        cpi = d.get("cpiv_img")
        if cpiv is None or cpi is None:
            l0, b0, r0, t0 = d["crop0"]
            cpi = (((l0 + r0) * 0.5 - 0.5) * iw,
                   ((b0 + t0) * 0.5 - 0.5) * ih)
            s0 = d["scale0"]
            pc, ps = math.cos(d["rot0"]), math.sin(d["rot0"])
            cpiv = (d["pos0"][0] * cw +
                    cpi[0] * s0[0] * pc - cpi[1] * s0[1] * ps,
                    d["pos0"][1] * ch +
                    cpi[0] * s0[0] * ps + cpi[1] * s0[1] * pc)
        pix, piy = cpi
        s0 = d["scale0"]
        if mode == 'rotate':
            a = math.atan2(cmy - cpiv[1], cmx - cpiv[0])
            rot = d["rot0"] + (a - d["angle0"])
            if event is not None and event.ctrl:
                # Absolute snap: quantize the result, not the delta.
                step = math.radians(5.0)
                rot = step * round(rot / step)
            rc, rs = math.cos(rot), math.sin(rot)
            nsx, nsy = s0
            item.rotation = rot
        else:
            # Horizontal rate control: left shrinks, right grows,
            # exponential (~2x per 250px).
            f = 2.0 ** ((mx - sm0[0]) / 250.0)
            nsx = max(0.01, s0[0] * f)
            nsy = max(0.01, s0[1] * f)
            rc, rs = math.cos(d["rot0"]), math.sin(d["rot0"])
            item.scale = (nsx, nsy)
        # Re-anchor pos so the pivot texel stays at cpiv on screen.
        item.pos = (
            (cpiv[0] - (pix * nsx * rc - piy * nsy * rs)) / cw,
            (cpiv[1] - (pix * nsx * rs + piy * nsy * rc)) / ch)
    elif mode == 'opacity':
        # Right-button rate control: right = more opaque, left = less, ~300px
        # for the full range. The 5% floor keeps the image from going
        # invisible. The % readout is refreshed every move so it stays up
        # while dragging and lingers ~1s after release.
        item.opacity = min(1.0, max(0.01,
                                    d["op0"] + (mx - sm0[0]) / 300.0))
        global _refboard_opacity_label
        _refboard_opacity_label = (region.as_pointer(), idx,
                                  time.time() + 1.0)
    elif mode == 'crop':
        # Slide the edge along its SCREEN-space normal: the visible window
        # moves parallel to itself, then both endpoints map back through
        # the inverse transform. For a rotated/marquee crop the UV quad is
        # sheared and a UV-space slide skews the edge - screen space is
        # the frame the user actually sees.
        p0 = d.get("crop_uv0") or tuple(
            v for p in _refboard_crop_uvs(item, crop=d["crop0"]) for v in p)
        pts = [(p0[0], p0[1]), (p0[2], p0[3]), (p0[4], p0[5]),
               (p0[6], p0[7])]
        # sub: 0 left (pts 3-0), 1 bottom (0-1), 2 right (1-2), 3 top (2-3)
        ai, bi = {0: (3, 0), 1: (0, 1), 2: (1, 2), 3: (2, 3)}[d["sub"]]
        oi = ((ai + 2) % 4, (bi + 2) % 4)
        view = _refboard_view(scene, region)
        sq = [_refboard_uv_to_screen(item, region, view, iw, ih, u, v)
              for u, v in pts]
        sa, sb = sq[ai], sq[bi]
        ex, ey = sb[0] - sa[0], sb[1] - sa[1]
        el = max(1e-9, math.hypot(ex, ey))
        nx, ny = -ey / el, ex / el
        omx = (sq[oi[0]][0] + sq[oi[1]][0]) * 0.5
        omy = (sq[oi[0]][1] + sq[oi[1]][1]) * 0.5
        emx, emy = (sa[0] + sb[0]) * 0.5, (sa[1] + sb[1]) * 0.5
        dist_opp = (omx - emx) * nx + (omy - emy) * ny
        if dist_opp < 0.0:
            nx, ny, dist_opp = -nx, -ny, -dist_opp
        off = min((mx - emx) * nx + (my - emy) * ny, dist_opp - 1.0)
        # The transform doesn't change during a crop drag, so a 1px step
        # along the normal maps to one UV delta shared by both endpoints.
        mu = _refboard_screen_to_uv(item, region, scene, emx, emy)
        nu = _refboard_screen_to_uv(item, region, scene,
                                    emx + nx, emy + ny)
        du, dv = nu[0] - mu[0], nu[1] - mu[1]
        # Per-corner slide deltas. Inward both take the shared normal
        # step so the edge stays parallel to itself. Outward (extending
        # an already-cropped edge back out) each corner instead rides its
        # ADJACENT side border's line: the quad grows along its own side
        # walls, so a corner can never shear across a side edge - and the
        # walls carry the corners to the original image border, which is
        # where the extension stops (never into empty canvas).
        da = db = (du, dv)
        lo2 = -1e30
        if off < 0.0:
            exu = pts[bi][0] - pts[ai][0]
            eyu = pts[bi][1] - pts[ai][1]
            cde = du * eyu - dv * exu
            # ai's side wall runs prev->ai (ends at ai); bi's runs
            # bi->next (starts at bi). Extend each past the corner.
            for k, o in ((ai, (ai - 1) % 4), (bi, (bi + 1) % 4)):
                wx = pts[k][0] - pts[o][0]
                wy = pts[k][1] - pts[o][1]
                cwe = wx * eyu - wy * exu
                if abs(cde) > 1e-12 and abs(cwe) > 1e-12:
                    f = cde / cwe
                    if k == ai:
                        da = (wx * f, wy * f)
                    else:
                        db = (wx * f, wy * f)
            # Side walls converging outward meet at an apex: stop before
            # the moving edge collapses to a point and the quad inverts.
            elu = math.hypot(exu, eyu)
            cff = (db[0] - da[0]) * exu + (db[1] - da[1]) * eyu
            if elu > 1e-9 and cff > 1e-12:
                lo2 = -(elu * elu * 0.999) / cff
        # Clamp `off` to the range where BOTH moved corners stay inside
        # the texture square: the slide stops the moment the first corner
        # touches an image border - letting the free endpoint run on is
        # what skewed the edge.
        lo, hi = lo2, 1e30
        for p2, dd in ((pts[ai], da), (pts[bi], db)):
            for coord, delta in ((p2[0], dd[0]), (p2[1], dd[1])):
                if delta > 1e-12:
                    hi = min(hi, (1.0 - coord) / delta)
                    lo = max(lo, -coord / delta)
                elif delta < -1e-12:
                    hi = min(hi, -coord / delta)
                    lo = max(lo, (1.0 - coord) / delta)
        off = max(lo, min(hi, off))
        # Inward slide on a rotated (sheared) quad: the screen-normal step
        # maps to a UV direction with a lateral component, so an endpoint
        # can shoot past an ADJACENT border of the drag-start quad and
        # flip the new quad into a bowtie - the skew seen when re-cropping
        # a rotated, already-cropped ref. Stop the edge the moment either
        # endpoint would cross ANY border of the original quad (convex
        # half-plane test; the centroid fixes the inside sign). Outward
        # moves are bounded by the texture clamp above instead.
        if off > 0.0:
            qcx = sum(pt[0] for pt in pts) * 0.25
            qcy = sum(pt[1] for pt in pts) * 0.25
            for k in (ai, bi):
                pqx, pqy = pts[k]
                for j in range(4):
                    jax, jay = pts[j]
                    jex = pts[(j + 1) % 4][0] - jax
                    jey = pts[(j + 1) % 4][1] - jay
                    cs = jex * (qcy - jay) - jey * (qcx - jax)
                    if abs(cs) < 1e-12:
                        continue
                    sgn = 1.0 if cs > 0.0 else -1.0
                    c0 = sgn * (jex * (pqy - jay) - jey * (pqx - jax))
                    c1 = sgn * (jex * dv - jey * du)
                    # inside the edge's half-plane: c0 + off * c1 >= 0
                    # c1 ~ |edge|*|delta| ~1e-3 for a real approach;
                    # near-parallel drift is float noise (~1e-9), not an
                    # exit - treat anything under 1e-7 as parallel.
                    if c1 < -1e-7:
                        off = max(0.0, min(off, c0 / -c1))
                    elif c0 < -1e-7:
                        off = 0.0
        pa, pb = pts[ai], pts[bi]
        pts[ai] = (pa[0] + da[0] * off, pa[1] + da[1] * off)
        pts[bi] = (pb[0] + db[0] * off, pb[1] + db[1] * off)
        _refboard_crop_commit(item, pts)


def _refboard_group_drag_update(scene, region, d, mx, my, event):
    """Collective edit: apply one shared transform (about the group bbox
    pivot chosen at drag start) to every member's stored state."""
    items = scene.refboard_items
    members = d.get("group_members") or []
    sm0 = d.get("m0_screen", d["m0"])
    if math.hypot(mx - sm0[0], my - sm0[1]) > 3.0:
        d["moved"] = True
    mode = d["mode"]
    rw, rh = _refboard_ref_size(scene, region)
    cmx, cmy = _refboard_to_canvas_px(region, scene, mx, my)
    if mode == 'group_inside':
        dx = (cmx - d["m0"][0]) / rw
        dy = (cmy - d["m0"][1]) / rh
        for i, p0, _s0, _r0, _c0, _o0 in members:
            if i < len(items):
                items[i].pos = (p0[0] + dx, p0[1] + dy)
    elif mode in ('group_corner', 'group_edge', 'group_center_scale'):
        if mode == 'group_center_scale':
            f = 2.0 ** ((mx - sm0[0]) / 250.0)
            ax, ay = d["center"]
        elif mode == 'group_corner':
            ax, ay = d["pivot"]
            gx, gy = d["grab"]
            dxv, dyv = gx - ax, gy - ay
            den = max(1e-4, dxv * dxv + dyv * dyv)
            f = ((cmx - ax) * dxv + (cmy - ay) * dyv) / den
        else:
            grect = d["grect"]
            ax, ay = d["pivot"]
            ex = max(1e-4, 2.0 * grect[2])
            ey = max(1e-4, 2.0 * grect[3])
            sub = d["sub"]
            if sub == 1:
                f = (cmx - ax) / ex
            elif sub == 3:
                f = (ax - cmx) / ex
            elif sub == 0:
                f = (ay - cmy) / ey
            else:
                f = (cmy - ay) / ey
        f = max(0.02, f)
        for i, p0, s0, _r0, _c0, _o0 in members:
            if i >= len(items):
                continue
            nx = ax + (p0[0] * rw - ax) * f
            ny = ay + (p0[1] * rh - ay) * f
            items[i].pos = (nx / rw, ny / rh)
            items[i].scale = (max(0.01, s0[0] * f),
                              max(0.01, s0[1] * f))
    elif mode == 'group_rotate':
        cx, cy = d["center"]
        a = math.atan2(cmy - cy, cmx - cx)
        delta = a - d["angle0"]
        if event is not None and event.ctrl:
            step = math.radians(5.0)
            delta = step * round(delta / step)
        c, s = math.cos(delta), math.sin(delta)
        for i, p0, _s0, r0, _c0, _o0 in members:
            if i >= len(items):
                continue
            vx, vy = p0[0] * rw - cx, p0[1] * rh - cy
            nx = cx + vx * c - vy * s
            ny = cy + vx * s + vy * c
            items[i].pos = (nx / rw, ny / rh)
            items[i].rotation = r0 + delta
    elif mode == 'group_opacity':
        delta = (mx - sm0[0]) / 300.0
        for i, _p0, _s0, _r0, _c0, o0 in members:
            if i < len(items):
                items[i].opacity = min(1.0, max(0.01, o0 + delta))
        global _refboard_opacity_label
        _refboard_opacity_label = (region.as_pointer(), -1,
                                  time.time() + 1.0)


# --- neighbor yield -------------------------------------------------------------
#
# While a scale drag pushes a ref into its neighbors, the neighbors yield:
# they shrink (and shoulder aside a little) until the intruder fits again.
# Targets come from a separating-axis relaxation and are integrated with an
# exponential lerp, so the response is fluid rather than a snap; shrinking
# the intruder back eases them toward their pre-drag state, and Esc returns
# them the same way.

_REFBOARD_YIELD_TAU = 0.09       # lerp time constant, seconds
_REFBOARD_YIELD_MARGIN = 6.0     # canvas px of gap targets aim for
_REFBOARD_YIELD_MIN = 0.6       # floor: 60% of the pre-drag scale
_REFBOARD_YIELD_PUSH = 0.3       # share of the clearance done by moving

# Scale drags only: moving/rotating/cropping over a neighbor is normal
# layering and must not disturb it.
_REFBOARD_YIELD_MODES = frozenset((
    'corner', 'edge', 'center_scale',
    'group_corner', 'group_edge', 'group_center_scale'))

_refboard_yield = None        # {"base": {idx: (sx, sy, px, py)}, "restore": bool}
_refboard_yield_clock = 0.0


def _refboard_sat_mtv(quad, other):
    """Minimum translation separating two convex quads: (depth, nx, ny) with
    the axis oriented from `other`'s centroid toward `quad`'s. None when
    they do not overlap."""
    best = None
    for poly in (quad, other):
        n = len(poly)
        for i in range(n):
            ex = poly[(i + 1) % n][0] - poly[i][0]
            ey = poly[(i + 1) % n][1] - poly[i][1]
            nx, ny = -ey, ex
            nl = math.hypot(nx, ny)
            if nl < 1e-9:
                continue
            nx, ny = nx / nl, ny / nl
            av = [p[0] * nx + p[1] * ny for p in quad]
            bv = [p[0] * nx + p[1] * ny for p in other]
            depth = min(max(av), max(bv)) - max(min(av), min(bv))
            if depth <= 0.0:
                return None
            if best is None or depth < best[0]:
                best = (depth, nx, ny)
    if best is None:
        return None
    acx = sum(p[0] for p in quad) / len(quad)
    acy = sum(p[1] for p in quad) / len(quad)
    bcx = sum(p[0] for p in other) / len(other)
    bcy = sum(p[1] for p in other) / len(other)
    if (acx - bcx) * best[1] + (acy - bcy) * best[2] < 0.0:
        best = (best[0], -best[1], -best[2])
    return best


def _refboard_overlap_push(quad, other):
    """(depth, nx, ny) for overlapped quads, measured along the
    centroid-centroid axis rather than the minimum SAT axis.

    SAT's shallowest axis can pick a sideways escape the user never pushed
    toward (e.g. shoving a right-hand neighbor *down* when the intruder
    grows into it horizontally). The approach axis reads naturally: the
    neighbor yields along the direction it is being crowded from.
    """
    if _refboard_sat_mtv(quad, other) is None:
        return None
    acx = sum(p[0] for p in quad) / len(quad)
    acy = sum(p[1] for p in quad) / len(quad)
    bcx = sum(p[0] for p in other) / len(other)
    bcy = sum(p[1] for p in other) / len(other)
    nx, ny = acx - bcx, acy - bcy
    nl = math.hypot(nx, ny)
    if nl < 1e-6:
        # Concentric: degenerate axis, fall back to the SAT direction.
        return _refboard_sat_mtv(quad, other)
    nx, ny = nx / nl, ny / nl
    ha = max(abs((p[0] - acx) * nx + (p[1] - acy) * ny) for p in quad)
    hb = max(abs((p[0] - bcx) * nx + (p[1] - bcy) * ny) for p in other)
    depth = ha + hb - nl
    if depth <= 0.0:
        return None
    return (depth, nx, ny)


_refboard_yield_timer_on = False


def _refboard_yield_timer():
    """App-timer driver for the neighbor-yield easing, registered only while
    a yield is live and self-unregistering once it settles. Runs outside the
    modal operator so no operator TIMER plumbing is needed."""
    global _refboard_yield, _refboard_yield_timer_on
    if not _refboard_yield:
        _refboard_yield_timer_on = False
        return None
    scene = _refboard_yield.get("scene")
    region = _refboard_yield.get("region")
    if scene is None or region is None:
        _refboard_yield = None
        _refboard_yield_timer_on = False
        return None
    try:
        moving = _refboard_yield_tick(scene, region)
        if moving:
            region.tag_redraw()
    except Exception:
        # A destroyed region/scene (area closed mid-gesture) must not stick
        # around as a live RNA reference - end the easing instead.
        _refboard_yield = None
        _refboard_yield_timer_on = False
        return None
    return 1.0 / 60.0


def _refboard_yield_begin(scene, exclude, region=None):
    """Snapshot yieldable neighbors at scale-drag start. The snapshot is the
    restore target, which keeps yields reversible within one gesture."""
    global _refboard_yield, _refboard_yield_clock, _refboard_yield_timer_on
    if _refboard_yield is None:
        _refboard_yield = {"base": {}, "restore": False}
    _refboard_yield["restore"] = False
    _refboard_yield["scene"] = scene
    _refboard_yield["region"] = region
    _refboard_yield_clock = time.time()
    for it in scene.refboard_items:
        hs = it.home_scale
        if hs[0] <= 0.0 or hs[1] <= 0.0:
            it.home_scale = tuple(it.scale)
    if region is not None and not _refboard_yield_timer_on:
        try:
            bpy.app.timers.register(_refboard_yield_timer)
            _refboard_yield_timer_on = True
        except Exception:
            pass
    for i, it in enumerate(scene.refboard_items):
        if i in exclude or it.image is None or it.locked:
            continue
        if i not in _refboard_yield["base"]:
            _refboard_yield["base"][i] = (
                it.scale[0], it.scale[1], it.pos[0], it.pos[1])


def _refboard_yield_intruders(scene, region):
    """Canvas-space quads of the item(s) a scale drag is currently growing."""
    d = _refboard_drag
    if d is None or d["mode"] not in _REFBOARD_YIELD_MODES:
        return []
    view = _refboard_canvas_view(scene, region)
    if d.get("group_members"):
        idxs = [m[0] for m in d["group_members"]]
    else:
        idxs = [d["index"]]
    quads = []
    for i in idxs:
        if not (0 <= i < len(scene.refboard_items)):
            continue
        it = scene.refboard_items[i]
        if it.image is None:
            continue
        rect = _refboard_rect(it, region, it.image, view)
        if rect is not None:
            quads.append(_refboard_quad(rect))
    return quads


def _refboard_yield_target(scene, region, it, base, view, intruder_quads,
                           cw, ch):
    """(sx, sy, px, py) this neighbor is easing toward.

    Two hysteresis regimes avoid shrink/clear oscillation: while overlapped
    the target relaxes deeper from the *current* quad; once clear, it is a
    static 'base, shrunk just enough' computed from the base quad - which
    converges back to base itself as the intrusion recedes.

    The split matters: shrink covers a preferred share of the clearance,
    and push absorbs ALL of the rest, so a target that 'clears' really does
    clear. A push share alone would leave the static target overlapping and
    the two regimes would ping-pong forever."""
    bsx, bsy, bpx, bpy = base
    if _refboard_yield["restore"] or it.image is None or not it.visible:
        return bsx, bsy, bpx, bpy
    iw, ih = it.image.size[0], it.image.size[1]
    if iw < 1 or ih < 1:
        return bsx, bsy, bpx, bpy
    rc, rs = math.cos(it.rotation), math.sin(it.rotation)
    uvs = _refboard_crop_uvs(it)

    def quad_for(sx, sy, px, py):
        out = []
        for u, v in uvs:
            lx = (u - 0.5) * iw * sx
            ly = (v - 0.5) * ih * sy
            out.append((px * cw + lx * rc - ly * rs,
                        py * ch + lx * rs + ly * rc))
        return out

    def deepest(q):
        best = None
        for iq in intruder_quads:
            mtv = _refboard_overlap_push(q, iq)
            if mtv is not None and (best is None or mtv[0] > best[0]):
                best = mtv
        return best

    quad = quad_for(it.scale[0], it.scale[1], it.pos[0], it.pos[1])
    best = deepest(quad)
    if best is not None:
        # Overlapped now: relax deeper from the current geometry.
        sx, sy, px, py = it.scale[0], it.scale[1], it.pos[0], it.pos[1]
    else:
        # Clear now: would the pre-drag state still overlap? If so the
        # target is base shrunk/moved just enough; otherwise it is base.
        bquad = quad_for(bsx, bsy, bpx, bpy)
        best = deepest(bquad)
        if best is None:
            return bsx, bsy, bpx, bpy
        quad = bquad
        sx, sy, px, py = bsx, bsy, bpx, bpy

    pen, nx, ny = best[0] + _REFBOARD_YIELD_MARGIN, best[1], best[2]
    qcx = sum(p[0] for p in quad) * 0.25
    qcy = sum(p[1] for p in quad) * 0.25
    h = max(abs((p[0] - qcx) * nx + (p[1] - qcy) * ny) for p in quad)
    h = max(1e-4, h)
    # How much of the axis footprint shrink can still cover before the
    # scale floor is reached. h scales with scale, so the floored
    # half-extent is h_authored * MIN ~ h * hsx / sx * MIN. The anchor is
    # the authored scale (home_scale), NOT this gesture's base: re-basing
    # each gesture on the shrunk state would compound the shrink forever.
    hsx = it.home_scale[0]
    hsx = hsx if hsx > 0.0 else bsx
    h_floor = h * (hsx / max(1e-6, sx)) * _REFBOARD_YIELD_MIN
    shrink_px = min(pen * (1.0 - _REFBOARD_YIELD_PUSH),
                    max(0.0, h - h_floor))
    f = (h - shrink_px) / h
    push = pen - shrink_px
    return (max(0.01, sx * f), max(0.01, sy * f),
            px + nx * push / max(1.0, cw),
            py + ny * push / max(1.0, ch))


def _refboard_yield_solve(scene, region, intruder_quads, dt=1.0 / 60.0,
                          snap=False):
    """Advance every yielding neighbor one step toward its target. Returns
    True while anything is still moving."""
    global _refboard_yield
    if not _refboard_yield:
        return False
    items = scene.refboard_items
    base = _refboard_yield["base"]
    cw, ch = _refboard_ref_size(scene, region)
    view = _refboard_canvas_view(scene, region)
    a = 1.0 if snap else 1.0 - math.exp(
        -max(0.0, dt) / _REFBOARD_YIELD_TAU)
    moving = False
    for i in list(base):
        if i >= len(items):
            base.pop(i, None)
            continue
        it = items[i]
        b = base[i]
        tsx, tsy, tpx, tpy = _refboard_yield_target(
            scene, region, it, b, view, intruder_quads, cw, ch)
        # Residual in roughly comparable units: scale relative to base,
        # pos in canvas px.
        rs = max(abs(tsx - it.scale[0]) / max(1e-4, b[0]),
                 abs(tsy - it.scale[1]) / max(1e-4, b[1]))
        rp = math.hypot((tpx - it.pos[0]) * cw, (tpy - it.pos[1]) * ch)
        if rs > 1e-3 or rp > 0.05:
            moving = True
        it.scale = (max(0.01, it.scale[0] + (tsx - it.scale[0]) * a),
                    max(0.01, it.scale[1] + (tsy - it.scale[1]) * a))
        it.pos = (it.pos[0] + (tpx - it.pos[0]) * a,
                  it.pos[1] + (tpy - it.pos[1]) * a)
    if _refboard_yield["restore"] and not moving:
        _refboard_yield = None
    return moving


def _refboard_yield_tick(scene, region):
    """One animation step, called on the modal timer and on drag updates."""
    global _refboard_yield_clock
    if not _refboard_yield:
        return False
    now = time.time()
    dt = now - _refboard_yield_clock if _refboard_yield_clock else 1.0 / 60.0
    _refboard_yield_clock = now
    return _refboard_yield_solve(
        scene, region, _refboard_yield_intruders(scene, region), dt)


def _refboard_yield_end(scene, region, cancel=False):
    """Drag over. Release converges the relaxation and snaps neighbors to
    their resolved targets (so the undo push records final values); Esc
    leaves them easing back to the pre-drag state."""
    global _refboard_yield
    if not _refboard_yield:
        return
    if cancel:
        _refboard_yield["restore"] = True
        return
    quads = _refboard_yield_intruders(scene, region)
    for _ in range(240):
        if not _refboard_yield_solve(scene, region, quads, snap=True):
            break
    base = _refboard_yield["base"]
    for i, it in enumerate(scene.refboard_items):
        if i not in base:
            it.home_scale = tuple(it.scale)
    _refboard_yield = None


def _refboard_board_state(scene):
    """Serializable snapshot of the board for the local undo stacks: every
    item's transform, crop quad, flags and its image (recorded by name AND
    filepath so a GC'd datablock can be reloaded), plus selection/group."""
    items = []
    for it in scene.refboard_items:
        img = it.image
        items.append((
            it.name,
            img.name if img is not None else "",
            (img.filepath_raw or "") if img is not None else "",
            tuple(it.pos), tuple(it.scale), it.rotation,
            tuple(it.crop), tuple(it.crop_pts),
            it.opacity, it.visible, it.locked,
            it.flip_x, it.flip_y, tuple(it.home_scale),
        ))
    return (tuple(items), scene.refboard_selected, tuple(_refboard_group))


def _refboard_apply_board_state(scene, st):
    """Restore a snapshot taken by _refboard_board_state. `crop` is written
    before `crop_pts` because the crop setter re-syncs the quad."""
    items, sel, grp = st
    coll = scene.refboard_items
    coll.clear()
    for (name, iname, path, pos, scale, rot, crop, pts, op, vis,
         locked, fx, fy, home) in items:
        it = coll.add()
        img = bpy.data.images.get(iname) if iname else None
        if img is None and path:
            try:
                img = bpy.data.images.load(path, check_existing=True)
            except Exception:
                img = None
        it.image = img
        if name:
            it.name = name
        it.pos = pos
        it.scale = scale
        it.rotation = rot
        it.crop = crop
        it.crop_pts = pts
        it.opacity = op
        it.visible = vis
        it.locked = locked
        it.flip_x = fx
        it.flip_y = fy
        it.home_scale = home
    scene.refboard_selected = sel if sel < len(coll) else -1
    _refboard_group[:] = [i for i in grp if i < len(coll)]


def _refboard_undo_seed(scene):
    """Set the resting-state baseline the next push will store. Called when
    edit mode opens, when a file loads, and lazily before one-shot ops that
    can run outside edit mode."""
    u = _refboard_undo
    if u["scene"] != scene:
        u["scene"] = scene
        u["past"].clear()
        u["future"].clear()
        u["prev"] = None
    if u["prev"] is None:
        u["prev"] = _refboard_board_state(scene)


def _refboard_undo_reset():
    """Drop all recorded board history (file load / addon reload)."""
    u = _refboard_undo
    u["scene"] = None
    u["prev"] = None
    u["past"].clear()
    u["future"].clear()


def _refboard_undo_push(message):
    """Record a completed Refboard edit in the board-local undo stack.
    Live drags mutate props directly for speed; the push stores the
    gesture's PRE state (kept in `prev`) and clears the redo tail. Kept
    out of Blender's memfile queue so Ctrl+Z in edit mode only steps
    through board edits."""
    u = _refboard_undo
    scene = bpy.context.scene
    _refboard_undo_seed(scene)
    u["past"].append(u["prev"])
    if len(u["past"]) > 64:
        u["past"].pop(0)
    u["prev"] = _refboard_board_state(scene)
    u["future"].clear()


def _refboard_undo_step(scene, redo=False):
    """Step the board-local stacks: undo restores the newest stored
    pre-state, redo reapplies the newest undone state. Returns False when
    the chosen stack is empty."""
    u = _refboard_undo
    _refboard_undo_seed(scene)
    if redo:
        if not u["future"]:
            return False
        u["past"].append(_refboard_board_state(scene))
        st = u["future"].pop()
    else:
        if not u["past"]:
            return False
        u["future"].append(_refboard_board_state(scene))
        st = u["past"].pop()
    _refboard_apply_board_state(scene, st)
    u["prev"] = st
    return True


@persistent
def _refboard_undo_post_guard(*args):
    """Blender's memfile undo snapshots refboard_items along with the rest
    of the scene, so a scene-level Ctrl+Z would roll the board back too.
    After each undo/redo, re-pin the board to our stack's resting state
    (`prev`) whenever the memfile restore drifted it - the two undo
    systems stay fully independent."""
    try:
        scene = getattr(bpy.context, "scene", None)
        if scene is None:
            return
        _refboard_undo_seed(scene)
        prev = _refboard_undo["prev"]
        if prev is None or _refboard_board_state(scene) == prev:
            return
        _refboard_apply_board_state(scene, prev)
        _refboard_redraw_views()
    except Exception:
        pass


def _refboard_delete_selected(scene):
    sel = scene.refboard_selected
    if 0 <= sel < len(scene.refboard_items):
        img = scene.refboard_items[sel].image
        scene.refboard_items.remove(sel)
        scene.refboard_selected = -1
        _refboard_drop_image(img)
        return True
    return False


def _refboard_move_depth(scene, indices, front):
    """Restack refs: draw order is collection order, so the last index is
    the frontmost. `front=True` moves the members to the end (to front),
    False moves them to index 0 (to back); relative order is kept. Returns
    the members' new indices."""
    items = scene.refboard_items
    members = sorted(i for i in set(indices) if 0 <= i < len(items))
    if not members:
        return []
    if front:
        for j, i in enumerate(members):
            items.move(i - j, len(items) - 1)
        return list(range(len(items) - len(members), len(items)))
    for j, i in enumerate(reversed(members)):
        items.move(i + j, 0)
    return list(range(len(members)))


def _refboard_ensure_modal():
    """Spin up the interact modal in a 3D view if it isn't running."""
    global _refboard_modal_running
    if _refboard_modal_running:
        return
    try:
        wm = bpy.context.window_manager
    except Exception:
        return
    for w in wm.windows:
        screen = w.screen
        if screen is None:
            continue
        for a in screen.areas:
            if a.type != 'VIEW_3D':
                continue
            region = next((r for r in a.regions if r.type == 'WINDOW'), None)
            if region is None:
                continue
            try:
                with bpy.context.temp_override(
                        window=w, screen=screen, area=a, region=region):
                    bpy.ops.refboard.interact('INVOKE_DEFAULT')
                if _refboard_modal_running:
                    return
            except Exception:
                continue


_REFBOARD_UI_REGIONS = {
    'UI', 'TOOLS', 'TOOL_PROPS', 'HEADER', 'TOOL_HEADER', 'FOOTER',
    'NAV_BAR', 'EXECUTE', 'HUD', 'ASSET_SHELF', 'ASSET_SHELF_HEADER',
    'CHANNELS', 'TEMPORARY',
}


def _refboard_over_ui(area, event):
    """True when the cursor sits over a panel/header rather than the viewport.

    Region overlap is on by default, so the 3D view's WINDOW region extends
    *underneath* the sidebar and toolbar. Mouse coordinates inside the WINDOW
    region are therefore not enough to tell "over the viewport" from "over the
    N-panel", and without this check the modal swallows clicks aimed at
    Blender's own UI - the buttons highlight on hover but never fire.
    """
    if area is None:
        return False
    wx, wy = event.mouse_x, event.mouse_y
    for r in area.regions:
        if r.type not in _REFBOARD_UI_REGIONS:
            continue
        if r.width <= 1 or r.height <= 1:
            continue  # collapsed
        if r.x <= wx < r.x + r.width and r.y <= wy < r.y + r.height:
            return True
    return False


def _refboard_boot_timer():
    """Deferred modal starter: retries until a 3D view exists to host it."""
    global _refboard_modal_running
    if _refboard_modal_running:
        return None
    any_items = False
    for sc in bpy.data.scenes:
        if len(getattr(sc, "refboard_items", ())) > 0:
            any_items = True
            break
    if not any_items and not _refboard_canvas_on:
        return None
    _refboard_ensure_modal()
    return None if _refboard_modal_running else 0.5


def _refboard_kick_boot_timer():
    try:
        if not bpy.app.timers.is_registered(_refboard_boot_timer):
            bpy.app.timers.register(_refboard_boot_timer, first_interval=0.3)
    except Exception:
        pass


class REFBOARD_OT_interact(bpy.types.Operator):
    bl_idname = "refboard.interact"
    bl_label = "Refboard Interact"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        global _refboard_modal_running
        context.window_manager.modal_handler_add(self)
        # Flag the modal as live only after the handler is really added -
        # otherwise a failed add leaves _refboard_modal_running stuck True
        # and _refboard_ensure_modal never retries, silently killing all
        # board input (including edit-mode undo) for the session.
        _refboard_modal_running = True
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            return {'RUNNING_MODAL'}

    def _modal(self, context, event):
        global _refboard_modal_running, _refboard_drag, _refboard_hover
        global _refboard_pan_drag, _refboard_group, _refboard_marquee
        global _refboard_zoom_drag
        global _refboard_canvas_on, _refboard_help_ts, _refboard_mod_ctrl
        global _refboard_cropmarq, _refboard_cropedge_click
        global _refboard_flick
        scene = context.scene
        items = getattr(scene, "refboard_items", None) if scene else None
        if (items is None or len(items) == 0) and _refboard_drag is None \
                and not _refboard_canvas_on:
            _refboard_modal_running = False
            return {'FINISHED'}

        area = context.area
        region = context.region
        if area is None or region is None:
            # The handler's context is frozen to the 3D view it was
            # invoked on. A workspace/screen switch frees that area, so
            # context.area/region come back None on every event and the
            # modal can never interact again while _refboard_modal_running
            # stays True forever. Die and respawn on the new screen's
            # 3D view.
            _refboard_modal_running = False
            if _refboard_canvas_on or (items is not None and len(items) > 0):
                _refboard_ensure_modal()
                if not _refboard_modal_running:
                    _refboard_kick_boot_timer()
            return {'FINISHED'}
        # Pin the layout's canvas size on first contact so later window
        # resizes scale the whole board instead of restretching positions.
        if region is not None and region.type == 'WINDOW':
            _refboard_ensure_ref_size(scene, region, stable=True)
        # True only when the pointer is over the 3D view's WINDOW region -
        # the region type alone isn't enough: events over the N-panel /
        # header / other editors must fall through to their own UI.
        in_view = (area is not None and area.type == 'VIEW_3D' and
                   region is not None and region.type == 'WINDOW' and
                   0 <= event.mouse_region_x < region.width and
                   0 <= event.mouse_region_y < region.height and
                   not _refboard_over_ui(area, event))

        # Track Ctrl for crop mode; redraw so markers hide immediately on
        # press/release.
        ctrl_now = bool(getattr(event, "ctrl", False)) or (
            event.type in {'LEFT_CTRL', 'RIGHT_CTRL'} and
            event.value == 'PRESS')
        if ctrl_now != _refboard_mod_ctrl:
            _refboard_mod_ctrl = ctrl_now
            if area is not None:
                area.tag_redraw()

        if event.type == 'MOUSEMOVE':
            mx, my = event.mouse_region_x, event.mouse_region_y
            if _refboard_flick is not None:
                fl = _refboard_flick
                if in_view and not fl["done"]:
                    dx = mx - fl["m0"][0]
                    dy = my - fl["m0"][1]
                    if math.hypot(dx, dy) >= _REFBOARD_FLICK_DIST:
                        # Only a fast crossing fires; a slow drag past the
                        # threshold expires harmlessly instead.
                        if time.time() - fl["t0"] <= _REFBOARD_FLICK_TIME \
                                and 0 <= fl["idx"] < len(items):
                            it = items[fl["idx"]]
                            if abs(dx) >= abs(dy):
                                it.flip_x = not it.flip_x
                            else:
                                it.flip_y = not it.flip_y
                            _refboard_undo_push("Refboard Flip")
                            area.tag_redraw()
                        fl["done"] = True
                return {'RUNNING_MODAL'}
            if _refboard_cropmarq is not None and in_view:
                _refboard_cropmarq_update(scene, region, mx, my)
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            if _refboard_marquee is not None and in_view:
                _refboard_marquee["cur"] = (mx, my)
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            if _refboard_drag is not None and in_view:
                _refboard_drag_update(scene, region, mx, my, event)
                _refboard_yield_tick(scene, region)
                _refboard_hover = (region.as_pointer(),
                                  _refboard_drag["mode"],
                                  _refboard_drag["sub"]) \
                    if _refboard_drag["mode"] in (
                        'rotate', 'crop', 'corner', 'edge',
                        'group_corner', 'group_edge') else None
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            if _refboard_pan_drag is not None and in_view:
                pd = _refboard_pan_drag
                pw, ph = _refboard_ref_size(scene, region)
                pk = _refboard_fit(scene, region)
                scene.refboard_view_pan = (
                    pd["pan0"][0] + (mx - pd["m0"][0]) / (pw * pk),
                    pd["pan0"][1] + (my - pd["m0"][1]) / (ph * pk))
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            if _refboard_zoom_drag is not None and in_view:
                zd = _refboard_zoom_drag
                zoom = min(20.0, max(0.05, zd["zoom0"] * math.exp(
                    (mx - zd["m0"][0]) / 200.0)))
                # Keep the canvas point under the press position pinned,
                # same as the wheel's zoom-toward-cursor behaviour.
                _refboard_zoom_anchor(scene, region, zd["m0"][0],
                                      zd["m0"][1], zd["c0"][0],
                                      zd["c0"][1], zoom)
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            if in_view and _refboard_canvas_on and (_refboard_group or
                            0 <= scene.refboard_selected < len(items)):
                if _refboard_group:
                    rect = _refboard_group_rect_screen(scene, region)
                else:
                    item = items[scene.refboard_selected]
                    rect = _refboard_rect(item, region, item.image,
                                         _refboard_view(scene, region)) \
                        if item.image is not None else None
                if _refboard_mod_ctrl and not _refboard_group and \
                        rect is not None:
                    z = _refboard_crop_edge_zone(
                        item, region, _refboard_view(scene, region),
                        mx, my)
                    if z is None:
                        z = _refboard_zone(rect, mx, my, False)
                else:
                    z = _refboard_zone(rect, mx, my, True,
                                      _refboard_mod_ctrl) \
                        if rect is not None else None
                zname = z[0] if z is not None else None
                if _refboard_group and zname == 'crop':
                    zname = 'inside'  # groups have no crop zones
                hv = (region.as_pointer(), zname, z[1]) \
                    if (z is not None and
                        zname in ('rotate', 'crop', 'corner', 'edge')) \
                    else None
                if hv != _refboard_hover:
                    _refboard_hover = hv
                    area.tag_redraw()
            elif _refboard_hover is not None:
                _refboard_hover = None
            if in_view and _refboard_canvas_on:
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}

        if event.type == 'ACCENT_GRAVE':
            # ` shows/hides the board, Alt+` enters edit mode, and either one
            # leaves edit mode. Only bare and Alt variants are ours - Ctrl /
            # Shift / OS combinations pass through to other bindings.
            if not in_view or event.ctrl or event.shift or event.oskey:
                return {'PASS_THROUGH'}
            if event.value == 'PRESS':
                _refboard_switch_mode(scene, event.alt)
                area.tag_redraw()
            # Swallowed either way, so the matching keymap item (the fallback
            # for when this modal is not running) cannot fire a second time.
            return {'RUNNING_MODAL'}

        if event.type == 'ESC':
            if _refboard_flick is not None:
                _refboard_flick = None
                return {'RUNNING_MODAL'}
            if _refboard_cropmarq is not None:
                _refboard_cropmarq = None
                if in_view:
                    area.tag_redraw()
                return {'RUNNING_MODAL'}
            if _refboard_marquee is not None:
                _refboard_marquee = None
                if in_view:
                    area.tag_redraw()
                return {'RUNNING_MODAL'}
            if _refboard_pan_drag is not None:
                scene.refboard_view_pan = _refboard_pan_drag["pan0"]
                _refboard_pan_drag = None
                if in_view:
                    area.tag_redraw()
                return {'RUNNING_MODAL'}
            if _refboard_drag is not None:
                d = _refboard_drag
                _refboard_drag = None
                if d.get("group_members"):
                    for i, p0, s0, r0, c0, o0 in d["group_members"]:
                        if i < len(scene.refboard_items):
                            it = scene.refboard_items[i]
                            it.pos, it.scale, it.rotation, it.crop, \
                                it.opacity = p0, s0, r0, c0, o0
                elif d["index"] < len(scene.refboard_items):
                    _refboard_restore(scene.refboard_items[d["index"]], d["snap"])
                    if d["temp"]:
                        scene.refboard_selected = -1
                _refboard_yield_end(scene, region, cancel=True)
                if in_view:
                    area.tag_redraw()
                return {'RUNNING_MODAL'}
            if _refboard_canvas_on and in_view:
                _refboard_canvas_on = False
                _refboard_npanel_restore()
                scene.refboard_selected = -1
                _refboard_group = []
                _refboard_mode_ts = time.time()
                _refboard_mode_label = "Refboard Exit"
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}

        if event.type == 'MIDDLEMOUSE':
            # Canvas mode: while a live ref is selected MMB pans the board
            # instead of orbiting the 3D view.
            if event.value == 'RELEASE':
                if _refboard_pan_drag is not None:
                    _refboard_pan_drag = None
                    if in_view:
                        area.tag_redraw()
                    return {'RUNNING_MODAL'}
                return {'PASS_THROUGH'}
            if event.value != 'PRESS' or not in_view or \
                    _refboard_drag is not None or \
                    not _refboard_canvas_on:
                return {'PASS_THROUGH'}
            _refboard_pan_drag = {
                "pan0": tuple(scene.refboard_view_pan),
                "m0": (event.mouse_region_x, event.mouse_region_y),
            }
            return {'RUNNING_MODAL'}

        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                          'WHEELINMOUSE', 'WHEELOUTMOUSE'}:
            if event.value != 'PRESS' or not in_view or \
                    not _refboard_canvas_on:
                return {'PASS_THROUGH'}
            zoom0 = float(scene.refboard_view_zoom)
            step = 1.15
            zoom = zoom0 * step if event.type in (
                'WHEELUPMOUSE', 'WHEELINMOUSE') else zoom0 / step
            zoom = min(20.0, max(0.05, zoom))
            # Zoom toward the cursor: keep the canvas point under the
            # mouse fixed by re-solving pan after the zoom change.
            mx, my = event.mouse_region_x, event.mouse_region_y
            cmx, cmy = _refboard_to_canvas_px(region, scene, mx, my)
            _refboard_zoom_anchor(scene, region, mx, my, cmx, cmy, zoom)
            area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'RIGHTMOUSE':
            # Alt+right-drag zooms the board, mirroring the wheel.
            if event.value == 'RELEASE' and _refboard_zoom_drag is not None:
                _refboard_zoom_drag = None
                if in_view:
                    area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value == 'PRESS' and event.alt and in_view and \
                    _refboard_drag is None and _refboard_canvas_on:
                _refboard_zoom_drag = {
                    "zoom0": float(scene.refboard_view_zoom),
                    "m0": (event.mouse_region_x, event.mouse_region_y),
                    "c0": _refboard_to_canvas_px(
                        region, scene, event.mouse_region_x,
                        event.mouse_region_y),
                }
                return {'RUNNING_MODAL'}
            # Ctrl+right-drag anywhere in the viewport fades the selected
            # image. Plain right-click stays free for Blender's own bindings.
            if event.value == 'RELEASE' and _refboard_drag is not None and \
                    _refboard_drag.get("button") == 'RIGHTMOUSE':
                _refboard_yield_end(scene, region)
                _refboard_drag = None
                if d["moved"]:
                    _refboard_undo_push("Refboard Edit")
                if in_view:
                    area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value != 'PRESS' or not in_view or \
                    not _refboard_canvas_on:
                return {'PASS_THROUGH'}
            if not event.ctrl:
                # Canvas-mode RMB is ours: the Refboard context menu.
                _refboard_ctx["region"] = region
                _refboard_ctx["mouse"] = (event.mouse_region_x,
                                         event.mouse_region_y)
                try:
                    bpy.ops.wm.call_menu(name="REFBOARD_MT_ctx")
                except Exception:
                    pass
                return {'RUNNING_MODAL'}
            sel = scene.refboard_selected
            if _refboard_group:
                _refboard_group_start_drag(
                    scene, region, 'opacity', -1,
                    event.mouse_region_x, event.mouse_region_y,
                    'RIGHTMOUSE')
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            if 0 <= sel < len(scene.refboard_items) and \
                    scene.refboard_items[sel].image is not None:
                _refboard_start_drag(scene, region, sel, 'opacity', -1,
                                    event.mouse_region_x,
                                    event.mouse_region_y, 'RIGHTMOUSE')
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            # Nothing selected: Ctrl+RMB is a board-wide opacity drag -
            # every ref shifts by the same delta, keeping relative
            # opacities. Reuses the collective opacity math.
            members = [(i, tuple(it.pos), tuple(it.scale), it.rotation,
                        tuple(it.crop), it.opacity)
                       for i, it in enumerate(scene.refboard_items)
                       if it.image is not None]
            if not members:
                return {'PASS_THROUGH'}
            _refboard_drag_set({
                "index": -1,
                "mode": "group_opacity",
                "sub": -1,
                "temp": False,
                "moved": False,
                "m0": (event.mouse_region_x, event.mouse_region_y),
                "m0_screen": (event.mouse_region_x, event.mouse_region_y),
                "button": 'RIGHTMOUSE',
                "snap": None,
                "group_members": members,
            })
            area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            if event.value == 'DOUBLE_CLICK':
                # Ctrl + double-click anywhere on an image restores the
                # original full-frame crop.
                if not (in_view and _refboard_canvas_on and
                        event.ctrl and not event.shift):
                    return {'PASS_THROUGH'}
                _refboard_cropedge_click = None
                hit = _refboard_pick(
                    scene, region, event.mouse_region_x,
                    event.mouse_region_y, True)
                if hit is not None and isinstance(hit[0], int):
                    item = scene.refboard_items[hit[0]]
                    _refboard_crop_commit(
                        item, [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0),
                               (0.0, 1.0)])
                    _refboard_undo_push("Refboard Crop")
                    area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE' and _refboard_flick is not None:
                _refboard_flick = None
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE' and _refboard_cropmarq is not None:
                cm = _refboard_cropmarq
                _refboard_cropmarq = None
                x0, y0 = cm["m0"]
                x1, y1 = cm["cur"]
                did = False
                if math.hypot(x1 - x0, y1 - y0) > 3.0:
                    if cm.get("inside"):
                        did = _refboard_apply_crop_rect(
                            scene, cm["region"], cm["idx"],
                            x0, y0, x1, y1)
                    elif not cm.get("illegal") and \
                            cm.get("target") is not None:
                        # Outside start over an unrotated ref: the crop is
                        # the marquee's overlap with the visible rect.
                        ti = cm["target"]
                        items = scene.refboard_items
                        if 0 <= ti < len(items) and \
                                items[ti].image is not None:
                            q = _refboard_crop_quad(
                                items[ti], cm["region"], items[ti].image,
                                _refboard_view(scene, cm["region"]))
                            qx0 = min(pq[0] for pq in q)
                            qx1 = max(pq[0] for pq in q)
                            qy0 = min(pq[1] for pq in q)
                            qy1 = max(pq[1] for pq in q)
                            rx0 = max(min(x0, x1), qx0)
                            rx1 = min(max(x0, x1), qx1)
                            ry0 = max(min(y0, y1), qy0)
                            ry1 = min(max(y0, y1), qy1)
                            if rx1 - rx0 > 0.5 and ry1 - ry0 > 0.5:
                                did = _refboard_apply_crop_rect(
                                    scene, cm["region"], ti,
                                    rx0, ry0, rx1, ry1)
                if did:
                    _refboard_undo_push("Refboard Crop")
                if in_view:
                    area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE' and _refboard_marquee is not None:
                mq = _refboard_marquee
                _refboard_marquee = None
                x0, y0 = mq["m0"]
                x1, y1 = mq["cur"]
                if math.hypot(x1 - x0, y1 - y0) > 3.0:
                    members = _refboard_marquee_members(
                        scene, mq["region"], x0, y0, x1, y1)
                    if len(members) >= 2:
                        _refboard_group = members
                        scene.refboard_selected = -1
                    elif len(members) == 1:
                        _refboard_group = []
                        scene.refboard_selected = members[0]
                    else:
                        _refboard_group = []
                        scene.refboard_selected = -1
                if in_view:
                    area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE' and _refboard_drag is not None and \
                    _refboard_drag.get("button", 'LEFTMOUSE') == 'LEFTMOUSE':
                d = _refboard_drag
                _refboard_yield_end(scene, region)
                _refboard_drag = None
                # A click-drag on an unselected image starts as a scratch
                # move; once it moved, the selection sticks on release so
                # the image stays editable. Esc still restores + deselects.
                if d["temp"] and d["moved"] and \
                        d["index"] < len(scene.refboard_items):
                    scene.refboard_selected = d["index"]
                if d["moved"]:
                    _refboard_undo_push("Refboard Edit")
                if in_view:
                    area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value != 'PRESS' or not in_view:
                return {'PASS_THROUGH'}
            if _refboard_pan_drag is not None or \
                    _refboard_zoom_drag is not None:
                return {'RUNNING_MODAL'}  # swallow clicks mid-pan/zoom
            if event.alt and not event.ctrl and _refboard_canvas_on:
                # Alt+LMB is deliberately inert in edit mode: swallowed so it
                # neither edits the board nor triggers Blender's alt-click.
                return {'RUNNING_MODAL'}
            if not _refboard_canvas_on:
                # Outside canvas mode the board is display-only: the 3D
                # scene gets all left-button input.
                return {'PASS_THROUGH'}
            # Canvas mode owns the button: no scene click/box-select,
            # tweak drags, alt-look or GP strokes reach Blender.
            mx, my = event.mouse_region_x, event.mouse_region_y
            sel = scene.refboard_selected
            if event.ctrl and event.alt:
                if _refboard_group:
                    _refboard_group_start_drag(
                        scene, region, 'center_scale', -1, mx, my)
                    return {'RUNNING_MODAL'}
                if 0 <= sel < len(scene.refboard_items) and \
                        scene.refboard_items[sel].image is not None:
                    _refboard_start_drag(scene, region, sel,
                                        'center_scale', -1, mx, my)
                    return {'RUNNING_MODAL'}
            hit = _refboard_pick(scene, region, mx, my, _refboard_mod_ctrl)
            if hit is None:
                if scene.refboard_selected != -1:
                    scene.refboard_selected = -1
                    area.tag_redraw()
                _refboard_group = []
                if event.ctrl and not event.shift and not event.alt:
                    # Ctrl+drag from empty space is a crop probe: over an
                    # unrotated ref it crops to the overlap on release;
                    # overlapping a rotated ref flags the drag illegal (red
                    # marquee) and commits nothing.
                    _refboard_cropmarq = {
                        "ptr": region.as_pointer(),
                        "region": region,
                        "idx": None,
                        "inside": False,
                        "illegal": False,
                        "target": None,
                        "m0": (mx, my),
                        "cur": (mx, my),
                    }
                else:
                    # In canvas mode, LMB drag on empty space is the marquee.
                    _refboard_marquee = {
                        "ptr": region.as_pointer(),
                        "region": region,
                        "m0": (mx, my),
                        "cur": (mx, my),
                    }
                return {'RUNNING_MODAL'}
            idx, zone, sub = hit
            if event.ctrl and event.shift:
                # Ctrl+Shift flick: over an image, the press is owned - a
                # fast horizontal/vertical drag flips the image that way.
                if idx != 'group':
                    _refboard_group = []
                    scene.refboard_selected = idx
                    _refboard_flick = {
                        "ptr": region.as_pointer(),
                        "idx": idx,
                        "m0": (mx, my),
                        "t0": time.time(),
                        "done": False,
                    }
                    area.tag_redraw()
                    return {'RUNNING_MODAL'}
            if idx == 'group':
                _refboard_group_start_drag(scene, region, zone, sub,
                                          mx, my)
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            _refboard_group = []
            scene.refboard_selected = idx
            if event.ctrl and not event.shift and not event.alt:
                # Ctrl+double-click anywhere on an image restores the
                # full frame - edge or interior both count. Tracked
                # manually: the second press of a real double-click may
                # arrive as 'DOUBLE_CLICK' (handled above) or as another
                # 'PRESS' here.
                now = time.time()
                prev = _refboard_cropedge_click
                _refboard_cropedge_click = (now, mx, my, idx)
                if prev is not None and prev[3] == idx and \
                        now - prev[0] <= 0.35 and \
                        math.hypot(mx - prev[1], my - prev[2]) <= 8.0:
                    _refboard_cropedge_click = None
                    _refboard_crop_commit(
                        scene.refboard_items[idx],
                        [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0),
                         (0.0, 1.0)])
                    _refboard_undo_push("Refboard Crop")
                    area.tag_redraw()
                    return {'RUNNING_MODAL'}
            if event.ctrl and not event.alt and \
                    zone in ('inside', 'inside_temp'):
                # Ctrl+LMB in the image interior is the crop marquee: drag
                # out a screen-space rect, crop the image to it on release.
                # The rect is clamped inside the visible crop quad every
                # move, so it can never sweep off the image and skew the
                # crop. Pressing on a highlighted crop edge still edge-drags
                # instead. If m0 lands in the fitted rect's dead corner
                # (outside the real quad) the drag falls back to the
                # outside-start probe rules.
                _refboard_cropmarq = {
                    "ptr": region.as_pointer(),
                    "region": region,
                    "idx": idx,
                    "inside": _refboard_point_in_poly(
                        mx, my, _refboard_crop_quad(
                            scene.refboard_items[idx], region,
                            scene.refboard_items[idx].image,
                            _refboard_view(scene, region))),
                    "illegal": False,
                    "target": None,
                    "m0": (mx, my),
                    "cur": (mx, my),
                }
                if not _refboard_cropmarq["inside"]:
                    _refboard_cropmarq["idx"] = None
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            if zone == 'inside' and idx != sel:
                zone = 'inside_temp'
            _refboard_start_drag(scene, region, idx, zone, sub, mx, my)
            area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type in {'Z', 'Y'} and event.value == 'PRESS' and \
                event.ctrl and not event.alt:
            # Board-local undo/redo - the stacks only exist for Refboard
            # edits, and only edit mode routes the key here. Outside edit
            # mode the binding isn't ours and Blender's undo runs.
            if not in_view or not _refboard_canvas_on:
                return {'PASS_THROUGH'}
            if _refboard_drag is None and _refboard_marquee is None and \
                    _refboard_cropmarq is None:
                redo = event.shift or event.type == 'Y'
                _refboard_undo_step(scene, redo=redo)
                area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'H' and event.value == 'PRESS':
            if not in_view or not _refboard_canvas_on:
                return {'PASS_THROUGH'}
            try:
                prefs = _get_prefs(context)
                prefs.show_help = not prefs.show_help
            except Exception:
                pass
            return {'RUNNING_MODAL'}

        if event.type in {'C', 'V'} and event.value == 'PRESS':
            if not in_view or not _refboard_canvas_on:
                return {'PASS_THROUGH'}
            if event.ctrl and not event.shift and not event.alt:
                if event.type == 'V':
                    # Same path as the Ctrl+V keymap item, driven from
                    # here because canvas mode swallows keymap input. The
                    # key stays consumed either way, so Blender's object
                    # paste can't fire in edit mode. With an image the op
                    # runs nested (keeps the cursor position); without one
                    # it's deferred to a timer so the report toast goes
                    # through normal dispatch, with status_text_set as a
                    # second native channel.
                    if _refboard_clipboard_has_image():
                        try:
                            bpy.ops.refboard.paste('INVOKE_DEFAULT')
                        except Exception:
                            pass
                    else:
                        _refboard_status(
                            "Refboard: no image on the clipboard")
                        try:
                            bpy.app.timers.register(
                                _refboard_report_no_image,
                                first_interval=0.0)
                        except Exception:
                            pass
                else:
                    _refboard_copy_selected(scene)
            return {'RUNNING_MODAL'}

        if event.type in {'DEL', 'X'} and event.value == 'PRESS':
            if not in_view or not _refboard_canvas_on:
                return {'PASS_THROUGH'}
            if _refboard_group:
                dropped = []
                for i in sorted(_refboard_group, reverse=True):
                    if i < len(scene.refboard_items):
                        dropped.append(scene.refboard_items[i].image)
                        scene.refboard_items.remove(i)
                _refboard_group = []
                _refboard_gc_images(dropped)
                _refboard_undo_push("Refboard Delete")
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            if in_view and 0 <= scene.refboard_selected < len(scene.refboard_items):
                _refboard_delete_selected(scene)
                _refboard_undo_push("Refboard Delete")
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}  # swallowed: not a scene delete

        if event.type in {'LEFT_BRACKET', 'RIGHT_BRACKET'} and \
                event.value == 'PRESS':
            # Figma-style restack: [ sends to the back, ] brings to front.
            if not in_view or not _refboard_canvas_on:
                return {'PASS_THROUGH'}
            front = event.type == 'RIGHT_BRACKET'
            sel = scene.refboard_selected
            if _refboard_group:
                _refboard_group = _refboard_move_depth(
                    scene, _refboard_group, front)
                _refboard_undo_push("Refboard Depth")
                area.tag_redraw()
                return {'RUNNING_MODAL'}
            if 0 <= sel < len(scene.refboard_items):
                new = _refboard_move_depth(scene, [sel], front)
                if new:
                    scene.refboard_selected = new[0]
                _refboard_undo_push("Refboard Depth")
                area.tag_redraw()
            return {'RUNNING_MODAL'}

        # Edit mode owns the viewport: every assigned gesture already
        # returned above, so anything left over (Blender hotkeys, scene
        # navigation, mouse traffic) is swallowed instead of leaking into
        # the 3D scene - empty board included, since canvas_mode() needs
        # items but the veil is up regardless. EVT_DROP (file drop onto a
        # region) must pass so it can reach the FileHandler dropboxes;
        # it isn't in the public event-type enum, so it may surface as
        # 'EVT_DROP' or as an empty identifier - allow both.
        if _refboard_canvas_on and in_view and \
                event.type not in {'EVT_DROP', 'NONE', ''}:
            return {'RUNNING_MODAL'}

        return {'PASS_THROUGH'}


# --- paste entry points ---------------------------------------------------------

class REFBOARD_MT_paste(bpy.types.Menu):
    bl_label = "Refboard"
    bl_idname = "REFBOARD_MT_paste"

    def draw(self, context):
        col = self.layout.column(align=True)
        col.operator("refboard.do_paste",
                     text="3D").mode = '3D'
        col.operator("refboard.do_paste",
                     text="Screen").mode = 'SCREEN'


class REFBOARD_MT_arrange(bpy.types.Menu):
    """Arrange submenu: layout ops for the marquee group."""
    bl_label = "Arrange"
    bl_idname = "REFBOARD_MT_arrange"

    def draw(self, context):
        col = self.layout.column()
        en = len(_refboard_group) >= 2
        row = col.row()
        row.enabled = en
        row.operator("refboard.arrange_auto",
                     text="Auto").dominant = False
        row = col.row()
        row.enabled = en
        row.operator("refboard.arrange_auto",
                     text="Selected Dominant").dominant = True


class REFBOARD_MT_ctx(bpy.types.Menu):
    """Canvas-mode right-click menu - the addon owns RMB there."""
    bl_label = "Refboard"
    bl_idname = "REFBOARD_MT_ctx"

    def draw(self, context):
        col = self.layout.column()
        col.menu("REFBOARD_MT_arrange")
        col.separator()
        col.operator("refboard.reset", text="Reset (Full)")
        col.operator("refboard.reset_crop", text="Reset (Cropping Only)")


def _refboard_reset_item(scene, idx):
    """Straighten the ref: rotation and flips back to neutral, opacity
    to full and the full uncropped frame. Size and position are kept
    as they are."""
    items = scene.refboard_items
    if not (0 <= idx < len(items)):
        return False
    item = items[idx]
    item.rotation = 0.0
    item.flip_x = item.flip_y = False
    item.opacity = 1.0
    _refboard_crop_commit(item, [(0.0, 0.0), (1.0, 0.0),
                                 (1.0, 1.0), (0.0, 1.0)])
    return True


def _refboard_reset_crop(scene, idx):
    """Restore the ref's full frame. Position, size and rotation keep
    their current values."""
    items = scene.refboard_items
    if not (0 <= idx < len(items)):
        return False
    _refboard_crop_commit(items[idx], [(0.0, 0.0), (1.0, 0.0),
                                       (1.0, 1.0), (0.0, 1.0)])
    return True


def _refboard_ctx_target(scene, region):
    """Ref the right-click menu acts on: the one under the cursor, else
    the current selection."""
    idx = scene.refboard_selected
    mouse = _refboard_ctx.get("mouse")
    if mouse is not None:
        hit = _refboard_pick(scene, region, mouse[0], mouse[1])
        if hit is not None and isinstance(hit[0], int):
            idx = hit[0]
    return idx


class REFBOARD_OT_reset(bpy.types.Operator):
    """Reset the ref under the cursor (or the selected ref): rotation,
    flips, cropping and opacity return to neutral. Size and position
    stay."""
    bl_idname = "refboard.reset"
    bl_label = "Reset (Full)"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        scene = context.scene
        region = _refboard_ctx.get("region") or context.region
        if region is None or region.type != 'WINDOW':
            return {'CANCELLED'}
        idx = _refboard_ctx_target(scene, region)
        if _refboard_reset_item(scene, idx):
            scene.refboard_selected = idx
            _refboard_undo_push("Refboard Reset")
            _refboard_redraw_views()
        return {'FINISHED'}


class REFBOARD_OT_reset_crop(bpy.types.Operator):
    """Restore the ref under the cursor (or the selected ref) to the
    full uncropped frame. Transform and opacity are untouched."""
    bl_idname = "refboard.reset_crop"
    bl_label = "Reset (Cropping Only)"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        scene = context.scene
        region = _refboard_ctx.get("region") or context.region
        if region is None or region.type != 'WINDOW':
            return {'CANCELLED'}
        idx = _refboard_ctx_target(scene, region)
        if _refboard_reset_crop(scene, idx):
            scene.refboard_selected = idx
            _refboard_undo_push("Refboard Reset Crop")
            _refboard_redraw_views()
        return {'FINISHED'}


class REFBOARD_OT_arrange_auto(bpy.types.Operator):
    """Pack the marquee group's refs inside its bounding box."""
    bl_idname = "refboard.arrange_auto"
    bl_label = "Arrange Auto"
    bl_options = {'INTERNAL'}

    dominant: bpy.props.BoolProperty(
        default=False,
        description="Grow the ref under the cursor so it reads as "
                    "dominant before packing")

    def execute(self, context):
        region = _refboard_ctx.get("region")
        if region is None:
            region = context.region
        if region is None or region.type != 'WINDOW':
            return {'CANCELLED'}
        if self.dominant:
            _refboard_make_dominant(
                context.scene, region, _refboard_ctx.get("mouse"))
        if _refboard_arrange_auto(context.scene, region):
            _refboard_undo_push("Refboard Arrange")
            _refboard_redraw_views()
        return {'FINISHED'}


class REFBOARD_OT_paste(bpy.types.Operator):
    """Ctrl+V entry point: gates on edit mode and an image actually being
    on the clipboard, stashes the view state under the mouse, and starts a
    screen-space import immediately."""
    bl_idname = "refboard.paste"
    bl_label = "Refboard"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        # Refboard must never hijack a paste outside edit mode:
        # PASS_THROUGH lets the keymap keep matching so Blender's own
        # paste (objects, drivers, ...) runs instead.
        if not _refboard_canvas_on:
            return {'PASS_THROUGH'}
        if not _refboard_clipboard_has_image():
            # Inside edit mode the key belongs to Refboard: no image means
            # "nothing to paste", not "defer to Blender's object paste".
            self.report({'INFO'}, "Refboard: no image on the clipboard")
            return {'CANCELLED'}
        pos = (0.5, 0.5)
        rw = rh = 0
        cw = ch = 0
        region = context.region
        if region is not None and region.type == 'WINDOW' and \
                region.width > 0 and region.height > 0:
            rw, rh = region.width, region.height
            scene = context.scene
            cw, ch = _refboard_ref_size(scene, region)
            cmx, cmy = _refboard_to_canvas_px(
                region, scene,
                event.mouse_region_x, event.mouse_region_y)
            pos = (cmx / cw, cmy / ch)
        try:
            basis = _refboard_view_basis(context)
        except Exception:
            basis = None
        global _refboard_menu_state
        _refboard_menu_state = {
            "pos": pos, "rw": rw, "rh": rh, "cw": cw, "ch": ch,
            "basis": basis,
            "window": context.window,
            "scene": context.scene,
            "collection": getattr(context, "collection", None),
        }
        # Screen-space only now; the 3D/Screen menu is kept around below
        # (do_paste still accepts mode='3D') but no longer invoked.
        return bpy.ops.refboard.do_paste(mode='SCREEN')


class REFBOARD_OT_do_paste(bpy.types.Operator):
    bl_idname = "refboard.do_paste"
    bl_label = "Paste Image"
    bl_options = {'INTERNAL'}

    mode: bpy.props.EnumProperty(
        items=(('3D', "3D Space", ""),
               ('SCREEN', "Screen Space", "")),
        default='SCREEN',
    )

    def execute(self, context):
        st = dict(_refboard_menu_state)
        fp = st.get("filepath")
        if fp:
            # Drag-drop: the file is already on disk, so skip the clipboard
            # subprocess and place it immediately.
            try:
                _refboard_finish({"dst": fp, "mode": self.mode, "state": st})
            except Exception as e:
                self.report({'ERROR'}, "Image load failed: %s" % e)
                return {'CANCELLED'}
            _refboard_redraw_views()
            return {'FINISHED'}
        dst = os.path.join(
            tempfile.gettempdir(),
            "refboard_%s.png" % uuid.uuid4().hex)
        # Clipboard-origin pastes get the icon-size floor at finish; drops
        # (state.filepath) keep accepting small files on purpose.
        st["clipboard"] = True
        # In-process clipboard read lands in ~ms, so the ref appears
        # immediately with no progress bar at all.
        if platform.system() == "Windows":
            try:
                hit = _refboard_clipboard_image_win(dst)
            except Exception:
                hit = None
            if hit:
                try:
                    _refboard_finish({"dst": hit, "mode": self.mode,
                                      "state": st})
                except Exception as e:
                    self.report({'ERROR'}, "Image load failed: %s" % e)
                    return {'CANCELLED'}
                _refboard_redraw_views()
                return {'FINISHED'}
        try:
            proc = _refboard_paste_proc(dst)
        except Exception as e:
            self.report({'ERROR'}, "Clipboard read failed: %s" % e)
            return {'CANCELLED'}
        _refboard_pending.append({
            "proc": proc, "dst": dst, "mode": self.mode,
            "state": st, "t0": time.time(), "shown": 0.0,
        })
        try:
            if not bpy.app.timers.is_registered(_refboard_poll_timer):
                bpy.app.timers.register(_refboard_poll_timer)
        except Exception:
            pass
        _refboard_redraw_views()
        return {'FINISHED'}


class REFBOARD_OT_drop(bpy.types.Operator):
    """File drop entry point: stashes the dropped image path and the view
    state under the cursor, then pops the same Refboard menu."""
    bl_idname = "refboard.drop"
    bl_label = "Refboard"
    bl_options = {'INTERNAL'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def _stash_and_menu(self, context, mx=None, my=None):
        fp = self.filepath
        if fp.lower().startswith(("http://", "https://")):
            # Browser drags hand us a URL, not a file. Fetch it to a temp
            # path and continue with the same menu flow.
            try:
                fp = _refboard_download_url(fp)
            except Exception as e:
                self.report({'ERROR'}, "Image download failed: %s" % e)
                return {'CANCELLED'}
        pos = (0.5, 0.5)
        rw = rh = 0
        cw = ch = 0
        region = context.region
        if region is not None and region.type == 'WINDOW' and \
                region.width > 0 and region.height > 0:
            rw, rh = region.width, region.height
            if mx is None:
                mx, my = rw * 0.5, rh * 0.5
            scene = context.scene
            cw, ch = _refboard_ref_size(scene, region)
            cmx, cmy = _refboard_to_canvas_px(region, scene, mx, my)
            pos = (cmx / cw, cmy / ch)
        try:
            basis = _refboard_view_basis(context)
        except Exception:
            basis = None
        global _refboard_menu_state
        _refboard_menu_state = {
            "pos": pos, "rw": rw, "rh": rh, "cw": cw, "ch": ch,
            "basis": basis,
            "window": context.window,
            "scene": context.scene,
            "collection": getattr(context, "collection", None),
            "filepath": fp,
        }
        return bpy.ops.refboard.do_paste(mode='SCREEN')

    def invoke(self, context, event):
        return self._stash_and_menu(
            context, event.mouse_region_x, event.mouse_region_y)

    def execute(self, context):
        return self._stash_and_menu(context)


class REFBOARD_FH_image(bpy.types.FileHandler):
    bl_idname = "REFBOARD_FH_image"
    bl_label = "Refboard"
    bl_import_operator = "refboard.drop"
    bl_file_extensions = (
        ".png;.jpg;.jpeg;.bmp;.tif;.tiff;.webp;.gif;.exr;.hdr")

    @classmethod
    def poll_drop(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'


class REFBOARD_OT_toggle_all(bpy.types.Operator):
    """Global eye: hides/shows every ref without touching per-item states."""
    bl_idname = "refboard.toggle_all"
    bl_label = "Toggle All Refs"

    def execute(self, context):
        scene = context.scene
        scene.refboard_all_hidden = not getattr(
            scene, "refboard_all_hidden", False)
        if scene.refboard_all_hidden:
            scene.refboard_selected = -1
        _refboard_redraw_views()
        return {'FINISHED'}


class REFBOARD_OT_clear(bpy.types.Operator):
    bl_idname = "refboard.clear"
    bl_label = "Clear Refs"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        scene = context.scene
        _refboard_undo_seed(scene)
        dropped = [it.image for it in scene.refboard_items]
        scene.refboard_items.clear()
        scene.refboard_selected = -1
        _refboard_group.clear()
        _refboard_gc_images(dropped)
        _refboard_undo_push("Refboard Clear")
        _refboard_redraw_views()
        return {'FINISHED'}


class REFBOARD_UL_refs(bpy.types.UIList):
    """Ref list rows: thumbnail, name, lock and eye toggles - rendered on
    the list widget's alternating row backgrounds like the outliner."""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        icon_val = 0
        if item.image is not None:
            try:
                item.image.preview_ensure()
                icon_val = item.image.preview.icon_id
            except Exception:
                icon_val = 0
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.prop(item, "name", text="", emboss=False,
                        placeholder=(item.image.name
                                     if item.image else "Ref"),
                        icon_value=icon_val)
            layout.prop(item, "locked", text="",
                        icon='LOCKED' if item.locked else 'UNLOCKED',
                        emboss=False)
            layout.prop(item, "visible", text="",
                        icon='HIDE_OFF' if item.visible else 'HIDE_ON',
                        emboss=False)
        else:  # GRID: bare thumbnail cell
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon_val)


class REFBOARD_PT_board(bpy.types.Panel):
    bl_label = "Refboard"
    bl_idname = "REFBOARD_PT_board"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Refboard"

    @classmethod
    def poll(cls, context):
        # The board controls live in add-on preferences; the N-panel section
        # only appears when 'Show N Panel' is enabled.
        return _refboard_pref("show_n_panel", True)

    def draw(self, context):
        scene = context.scene
        col = self.layout.column(align=True)
        row = col.row(align=True)
        row.label(text="Global Visibility")
        row.operator("refboard.toggle_all", text="",
                     icon='HIDE_ON'
                     if getattr(scene, "refboard_all_hidden", False)
                     else 'HIDE_OFF')
        items = getattr(scene, "refboard_items", None)
        sel = getattr(scene, "refboard_selected", -1)
        if items and 0 <= sel < len(items):
            col.prop(items[sel], "opacity", text="Image Opacity",
                     slider=True)
        if items:
            col.template_list(
                "REFBOARD_UL_refs", "", scene, "refboard_items",
                scene, "refboard_selected",
                rows=min(8, max(3, len(items))))
            col.operator("refboard.clear", text="Clear Refs",
                         icon='TRASH')

        box = self.layout.box()
        box.label(text="Image Repository", icon='FILE_FOLDER')
        repo = _refboard_repo_dir()
        if repo:
            packed = sum(1 for i in _refboard_tracked_images() if i.packed_file)
            box.label(text="//%s/" % _REFBOARD_REPO_NAME)
            if packed:
                box.label(text="%d packed in scene" % packed, icon='ERROR')
                box.operator("refboard.externalize", icon='EXPORT')
            else:
                box.label(text="All images external", icon='CHECKMARK')
        else:
            box.label(text="Unsaved file: images held in cache",
                      icon='INFO')


class REFBOARD_OT_toggle(bpy.types.Operator):
    """` / Alt+` mode switch.

    The interact modal handles these keys itself whenever it is running, and
    swallows them so this never double-fires. This exists for the case where the
    modal is not running - an empty board, or a session where it died - and it
    restarts the modal so the keys keep working from then on.
    """
    bl_idname = "refboard.toggle"
    bl_label = "Refboard Mode"

    alt: bpy.props.BoolProperty(
        default=False,
        description="Enter edit mode instead of toggling visibility")

    def execute(self, context):
        _refboard_switch_mode(context.scene, self.alt)
        _refboard_ensure_modal()
        _refboard_redraw_views()
        return {'FINISHED'}


@persistent
def _refboard_load_post(dummy):
    """Board contents are scene data and survive the reload; only the transient
    runtime pieces need resetting."""
    global _refboard_drag, _refboard_modal_running, _refboard_marquee
    global _refboard_cropmarq, _refboard_canvas_on, _refboard_cropedge_click
    global _refboard_hover, _refboard_pan_drag, _refboard_opacity_label
    global _refboard_mode_label, _refboard_zoom_drag, _refboard_flick
    _refboard_drag = None
    _refboard_modal_running = False
    _refboard_marquee = None
    _refboard_cropmarq = None
    _refboard_flick = None
    _refboard_canvas_on = False
    _refboard_npanel_restore()
    _refboard_cropedge_click = None
    _refboard_hover = None
    _refboard_pan_drag = None
    _refboard_zoom_drag = None
    _refboard_opacity_label = None
    _refboard_mode_label = ""
    _refboard_group.clear()
    _refboard_pending.clear()
    _refboard_icon_cache.clear()
    _refboard_undo_reset()
    _refboard_kick_boot_timer()


@persistent
def _refboard_save_pre(filepath):
    """Externalize images before the file is written, so the .blend on disk only
    ever holds short relative paths. The handler receives the path being saved,
    which is what makes Save As to a new folder land in the right repo."""
    try:
        _refboard_gc_images()
        _refboard_externalize(filepath or None)
    except Exception as e:
        print("Refboard: externalize on save failed:", e)


class REFBOARD_OT_externalize(bpy.types.Operator):
    """Write packed Refboard images out to the sibling repo folder"""
    bl_idname = "refboard.externalize"
    bl_label = "Externalize Images"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({'WARNING'},
                        "Save the .blend first so the repo folder has a home")
            return {'CANCELLED'}
        _refboard_gc_images()
        n = _refboard_externalize()
        self.report({'INFO'}, "Refboard: externalized %d image(s)" % n)
        return {'FINISHED'}


# --- self-update ------------------------------------------------------------

_REFBOARD_GH_REPO = "arieldiazj/refboard"
_refboard_update = {"state": "idle", "msg": ""}


def _refboard_parse_ver(name):
    """'v0.2.0' -> (0, 2, 0) padded to 3; anything unparseable -> None."""
    t = (name or "").strip().lower()
    if t.startswith('v'):
        t = t[1:]
    parts = t.split('.')
    if not parts or len(parts) > 4:
        return None
    try:
        v = tuple(int(p) for p in parts)
    except ValueError:
        return None
    return v + (0,) * (3 - len(v)) if len(v) < 3 else v


def _refboard_latest_tag(tags):
    """Max semver entry of a /tags API payload -> (version_tuple, tag_dict)."""
    best = None
    for t in tags or []:
        v = _refboard_parse_ver(t.get("name"))
        if v is not None and (best is None or v > best[0]):
            best = (v, t)
    return best


def _refboard_gh_get(url):
    import urllib.request
    req = urllib.request.Request(
        url, headers={"User-Agent": "refboard-addon-updater"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _refboard_install_zip(zip_path, addon_dir):
    """Overlay a tag zipball onto addon_dir after zipping the current files
    into _backups/ (built in temp first so the archive doesn't capture
    itself). The zip nests under <user>-<repo>-<sha>/, which is stripped."""
    ver = ".".join(str(x) for x in bl_info.get("version", (0, 0, 0)))
    fd, tmpzip = tempfile.mkstemp(suffix=".zip", prefix="refboard_bak_")
    os.close(fd)
    os.remove(tmpzip)
    shutil.make_archive(tmpzip[:-4], 'zip', addon_dir)
    bk = os.path.join(addon_dir, "_backups")
    os.makedirs(bk, exist_ok=True)
    shutil.move(tmpzip, os.path.join(
        bk, "refboard_v%s_%s.zip" % (ver, time.strftime("%Y%m%d_%H%M%S"))))
    with zipfile.ZipFile(zip_path) as z:
        roots = {n.split('/')[0] for n in z.namelist() if '/' in n}
        root = roots.pop() if len(roots) == 1 else ''
        for n in z.namelist():
            rel = n[len(root):].lstrip('/') if root and n.startswith(root) else n
            if not rel or n.endswith('/') or rel.startswith(('.git', '_backups')):
                continue
            dst = os.path.join(addon_dir, *rel.split('/'))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with z.open(n) as src, open(dst, 'wb') as out:
                shutil.copyfileobj(src, out)


def _refboard_update_worker():
    try:
        import json
        raw = _refboard_gh_get(
            "https://api.github.com/repos/%s/tags" % _REFBOARD_GH_REPO)
        best = _refboard_latest_tag(json.loads(raw.decode('utf8')))
        local = tuple(bl_info.get("version", (0, 0, 0)))
        if best is None:
            _refboard_update.update(
                state="done", msg="No tagged versions on GitHub yet.")
            return
        ver, tag = best
        vstr = ".".join(str(x) for x in ver)
        if ver <= local:
            _refboard_update.update(
                state="done",
                msg="Refboard is up to date (v%s)." %
                    ".".join(str(x) for x in local))
            return
        _refboard_update.update(state="busy", msg="Downloading v%s..." % vstr)
        zdata = _refboard_gh_get(tag.get("zipball_url"))
        zp = os.path.join(tempfile.gettempdir(), "refboard_update.zip")
        with open(zp, "wb") as f:
            f.write(zdata)
        _refboard_update.update(state="busy", msg="Installing v%s..." % vstr)
        _refboard_install_zip(zp, os.path.dirname(os.path.abspath(__file__)))
        _refboard_update.update(
            state="done",
            msg="Updated to v%s - restart Blender to load it." % vstr)
    except Exception as e:
        _refboard_update.update(state="error", msg="Update failed: %s" % e)


def _refboard_update_poll():
    try:
        for w in bpy.context.window_manager.windows:
            for a in (w.screen.areas if w.screen else ()):
                if a.type == 'PREFERENCES':
                    a.tag_redraw()
    except Exception:
        pass
    return 0.25 if _refboard_update.get("state") == "busy" else None


class REFBOARD_OT_update_check(bpy.types.Operator):
    """Check GitHub for a newer tagged version. Installs it over the addon
    (with a timestamped zip backup first) and asks for a restart. Never
    runs when the remote version is not strictly newer."""
    bl_idname = "refboard.update_check"
    bl_label = "Refboard Update"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _refboard_update.get("state") != "busy"

    def execute(self, context):
        _refboard_update.update(state="busy", msg="Checking GitHub...")
        threading.Thread(target=_refboard_update_worker, daemon=True).start()
        try:
            bpy.app.timers.register(_refboard_update_poll)
        except Exception:
            pass
        return {'FINISHED'}


classes = (
    RefboardItem,
    RefboardPreferences,
    REFBOARD_OT_externalize,
    REFBOARD_OT_interact,
    REFBOARD_OT_paste,
    REFBOARD_OT_do_paste,
    REFBOARD_OT_drop,
    REFBOARD_FH_image,
    REFBOARD_OT_toggle_all,
    REFBOARD_OT_toggle,
    REFBOARD_OT_clear,
    REFBOARD_OT_arrange_auto,
    REFBOARD_OT_update_check,
    REFBOARD_OT_reset,
    REFBOARD_OT_reset_crop,
    REFBOARD_MT_paste,
    REFBOARD_MT_arrange,
    REFBOARD_MT_ctx,
    REFBOARD_UL_refs,
    REFBOARD_PT_board,
)

_SCENE_PROPS = (
    "refboard_items", "refboard_selected", "refboard_all_hidden",
    "refboard_view_zoom", "refboard_view_pan",
)


def register():
    for c in classes:
        bpy.utils.register_class(c)

    bpy.types.Scene.refboard_items = bpy.props.CollectionProperty(
        type=RefboardItem)
    bpy.types.Scene.refboard_selected = bpy.props.IntProperty(
        default=-1,
        update=lambda self, context: _refboard_redraw_views())
    bpy.types.Scene.refboard_all_hidden = bpy.props.BoolProperty(default=False)
    # Board view: per-scene so a saved .blend restores the same framing.
    bpy.types.Scene.refboard_view_zoom = bpy.props.FloatProperty(
        name="Board Zoom", default=1.0, min=0.05, max=20.0,
        update=lambda self, context: _refboard_redraw_views())
    bpy.types.Scene.refboard_view_pan = bpy.props.FloatVectorProperty(
        name="Board Pan", size=2, default=(0.0, 0.0),
        update=lambda self, context: _refboard_redraw_views())
    # Canvas size the layout was authored at. (0,0) = unpinned, which
    # falls back to the live viewport until the board is next touched.
    bpy.types.Scene.refboard_ref_size = bpy.props.FloatVectorProperty(
        name="Canvas Size", size=2, default=(0.0, 0.0), min=0.0,
        update=lambda self, context: _refboard_redraw_views())
    bpy.app.handlers.load_post.append(_refboard_load_post)
    bpy.app.handlers.save_pre.append(_refboard_save_pre)
    bpy.app.handlers.undo_post.append(_refboard_undo_post_guard)
    bpy.app.handlers.redo_post.append(_refboard_undo_post_guard)

    _refboard_kick_boot_timer()

    global _REFBOARD_HANDLER
    if _REFBOARD_HANDLER is None:
        try:
            _REFBOARD_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
                _draw_refboard, (), 'WINDOW', 'POST_PIXEL'
            )
        except Exception as e:
            print("Refboard: could not add draw handler:", e)

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        try:
            km = kc.keymaps.new(name="3D View", space_type='VIEW_3D')
            paste = "oskey" if platform.system() == "Darwin" else "ctrl"
            kmi = km.keymap_items.new(
                "refboard.paste", type='V', value='PRESS',
                **{paste: True})
            _refboard_keymaps.append((km, kmi))
            kmi = km.keymap_items.new(
                "refboard.toggle_all", type='V', value='PRESS',
                ctrl=True, shift=True)
            _refboard_keymaps.append((km, kmi))
            # ` shows/hides the board, Alt+` goes to edit mode.
            kmi = km.keymap_items.new(
                "refboard.toggle", type='ACCENT_GRAVE', value='PRESS')
            kmi.properties.alt = False
            _refboard_keymaps.append((km, kmi))
            kmi = km.keymap_items.new(
                "refboard.toggle", type='ACCENT_GRAVE', value='PRESS',
                alt=True)
            kmi.properties.alt = True
            _refboard_keymaps.append((km, kmi))
        except Exception as e:
            print("Refboard: could not register keymap:", e)


def unregister():
    global _REFBOARD_HANDLER
    if _REFBOARD_HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                _REFBOARD_HANDLER, 'WINDOW')
        except Exception as e:
            print("Refboard: could not remove draw handler:", e)
        _REFBOARD_HANDLER = None

    for km, kmi in _refboard_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _refboard_keymaps.clear()

    for timer in (_refboard_poll_timer,
                  _refboard_boot_timer, _refboard_yield_timer):
        try:
            bpy.app.timers.unregister(timer)
        except Exception:
            pass
    _refboard_ref_pending.clear()
    global _refboard_yield_timer_on
    _refboard_yield_timer_on = False

    try:
        bpy.app.handlers.load_post.remove(_refboard_load_post)
    except ValueError:
        pass

    try:
        bpy.app.handlers.save_pre.remove(_refboard_save_pre)
    except ValueError:
        pass

    for h in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        try:
            h.remove(_refboard_undo_post_guard)
        except ValueError:
            pass

    global _refboard_drag, _refboard_modal_running, _refboard_hover
    global _refboard_opacity_label, _refboard_pan_drag, _refboard_marquee
    global _refboard_cropmarq, _refboard_canvas_on, _refboard_mod_ctrl
    global _refboard_cropedge_click, _refboard_mode_label
    global _REFBOARD_TEX_SHADER, _REFBOARD_FLAT_SHADER, _REFBOARD_SMOOTH_SHADER
    global _refboard_zoom_drag, _refboard_yield, _refboard_flick
    _refboard_yield = None
    _refboard_drag = None
    _refboard_hover = None
    _refboard_pan_drag = None
    _refboard_zoom_drag = None
    _refboard_marquee = None
    _refboard_cropmarq = None
    _refboard_flick = None
    _refboard_canvas_on = False
    _refboard_npanel_restore()
    _refboard_mod_ctrl = False
    _refboard_cropedge_click = None
    _refboard_mode_label = ""
    _refboard_opacity_label = None
    _refboard_modal_running = False
    _REFBOARD_TEX_SHADER = None
    _REFBOARD_FLAT_SHADER = None
    _REFBOARD_SMOOTH_SHADER = None
    _refboard_icon_cache.clear()
    _refboard_group.clear()
    _refboard_pending.clear()

    for prop in _SCENE_PROPS:
        try:
            delattr(bpy.types.Scene, prop)
        except AttributeError:
            pass

    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()

