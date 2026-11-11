"""Write the tray/exe icon as a multi-size .ico (used by the Windows build)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from seasoning.windows import _icon_image  # noqa: E402

_icon_image().save(sys.argv[1], sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
