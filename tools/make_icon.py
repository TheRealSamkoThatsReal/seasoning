"""Write the tray/exe icon as a multi-size .ico (used by the Windows build)."""
import sys

from seasoning.windows import _icon_image

_icon_image().save(sys.argv[1], sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
