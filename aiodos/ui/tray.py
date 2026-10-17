"""Icône d'AIODOS dans la zone de notification de Windows (docs/arriere-plan.md).

Fermer la fenêtre la cache seulement (réglage close_to_tray, cf. ui/window.py) : le
nœud continue de servir les machines appairées et d'avancer le travail de fond. Clic sur
l'icône : la fenêtre revient ; clic droit : « Ouvrir AIODOS », « Quitter AIODOS ».

L'icône est un NotifyIcon de WinForms, que pywebview charge déjà sous Windows pour la
fenêtre (pythonnet) : elle vit dans le même fil graphique qu'elle (create, appelée par
Invoke), et WinForms la remet en place si l'Explorateur redémarre. Ailleurs que sous
Windows, ou si WinForms manque, il n'y a pas d'icône : fermer la fenêtre arrête AIODOS,
comme avant.
"""
import logging
import sys

log = logging.getLogger("aiodos.tray")

TITLE = "AIODOS"


def available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import clr  # noqa: F401  (pythonnet, chargé par pywebview)
        return True
    except ImportError:
        return False


class Tray:
    """on_open() : montrer la fenêtre ; on_quit() : arrêter AIODOS. Toutes les méthodes
    s'appellent depuis le fil graphique (handlers de WinForms, Invoke)."""

    def __init__(self, icon_path, on_open, on_quit):
        self.icon_path = str(icon_path)
        self.on_open = on_open
        self.on_quit = on_quit
        self._icon = None

    def create(self):
        import clr
        clr.AddReference("System.Drawing")
        clr.AddReference("System.Windows.Forms")
        from System.Drawing import Font, FontStyle, Icon
        from System.Windows.Forms import (ContextMenuStrip, NotifyIcon, ToolStripMenuItem,
                                          ToolStripSeparator)
        menu = ContextMenuStrip()
        show = ToolStripMenuItem("Ouvrir AIODOS")
        show.Font = Font(show.Font, FontStyle.Bold)          # l'action du clic, en gras
        show.Click += lambda sender, args: self.on_open()
        leave = ToolStripMenuItem("Quitter AIODOS")
        leave.Click += lambda sender, args: self.on_quit()
        menu.Items.Add(show)
        menu.Items.Add(ToolStripSeparator())
        menu.Items.Add(leave)
        icon = NotifyIcon()
        icon.Icon = Icon(self.icon_path)
        icon.Text = TITLE
        icon.ContextMenuStrip = menu
        icon.MouseClick += self._clicked
        icon.Visible = True
        self._icon = icon

    def _clicked(self, sender, args):
        from System.Windows.Forms import MouseButtons
        if args.Button == MouseButtons.Left:
            self.on_open()

    def notify(self, text: str):
        """Bulle (notification de Windows) attachée à l'icône."""
        if self._icon is not None:
            from System.Windows.Forms import ToolTipIcon
            self._icon.ShowBalloonTip(6000, TITLE, text, ToolTipIcon.Info)

    def remove(self):
        """L'icône disparaît tout de suite (sinon elle resterait jusqu'au survol)."""
        icon, self._icon = self._icon, None
        if icon is not None:
            icon.Visible = False
            icon.Dispose()
