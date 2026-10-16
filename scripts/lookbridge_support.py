"""Optional support links shared by LookBridge's Resolve windows.

These are the same one-time QuickBooks checkout links used by Nacho Power.
Only an explicit click opens a checkout; LookBridge never submits payments.
"""

from __future__ import print_function

import subprocess
import sys
import webbrowser


APP_VERSION = "1.0.0"
UPDATE_URL = "lookbridge://check-for-updates"
SUPPORT_OPTIONS = (
    (5, "https://connect.intuit.com/pay/AtomFilmLabInc/scs-v1-ea4fa06c85294c0bba3e1bc44c31841b58a15d02de6747958704525045c9112d95a87fb2e2b541449d7f00193fcf05b0?locale=EN_US&cta=saveandcopylink"),
    (10, "https://connect.intuit.com/pay/AtomFilmLabInc/scs-v1-16dd11c181c448d88fb730fff2248be83f572df0d7434f148156c68d7ec6c05edc4f8a6ce74b48218231421dc1f05cce-1?locale=EN_US&cta=saveandcopylink"),
    (25, "https://connect.intuit.com/pay/AtomFilmLabInc/scs-v1-3dbf78b790fc4ae69f42a22f3f60b86a9a8c54d7e98f4e2fa9f749b01b5ecc2632ab2bd437b1489187f33fda7703649d-0?locale=EN_US&cta=saveandcopylink"),
)


def _active_windows():
    # Each bundled tool is loaded as a separate module. sys is shared by all
    # of them within Resolve's interpreter, including later menu invocations.
    name = "_lookbridge_active_windows"
    if not hasattr(sys, name):
        setattr(sys, name, {})
    return getattr(sys, name)


def active_window(identifier):
    """Return only a window whose owner still has an active UI session."""
    return _active_windows().get(identifier)


def register_window(identifier, window):
    _active_windows()[identifier] = window


def unregister_window(identifier, window):
    windows = _active_windows()
    if windows.get(identifier) is window:
        del windows[identifier]


def _open_url(url):
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/open", url], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, timeout=15,
        )
        if result.returncode:
            raise RuntimeError(
                "Could not open the link. Open LookBridge from Applications "
                "to check for updates, or try again from the launcher."
            )
        return True
    if not webbrowser.open(url):
        raise RuntimeError("Could not open the link in your default browser")
    return True


def open_support(amount):
    """Open one supported amount, without constructing an arbitrary URL."""
    for option_amount, url in SUPPORT_OPTIONS:
        if amount == option_amount:
            return _open_url(url)
    raise ValueError("Choose a support amount of $5, $10, or $25 USD")


def check_for_updates():
    return _open_url(UPDATE_URL)


def footer(ui):
    """Use compact flat buttons for reliable clickable text in Fusion UI."""
    links = [ui.Label({"Text": "Support (optional)", "Weight": 0})]
    for amount, _ in SUPPORT_OPTIONS:
        links.append(ui.Button({
            "ID": "LookBridgeSupport{}".format(amount),
            "Text": "${}".format(amount), "Flat": True, "Weight": 0,
            "MaximumSize": [48, 24],
            "ToolTip": "Optional one-time ${} USD support via QuickBooks".format(amount),
        }))
    links.extend([
        ui.HGap(1),
        ui.Button({
            "ID": "LookBridgeUpdates", "Text": "Check for updates",
            "Flat": True, "Weight": 0, "MaximumSize": [150, 24],
            "ToolTip": "Open LookBridge to check for and install updates",
        }),
    ])
    return ui.HGroup({"Weight": 0, "Spacing": 2}, links)


def bind_footer(window, on_error):
    """Wire links without allowing an external-app failure to close the UI."""
    def callback(action):
        def clicked(event):
            try:
                action()
            except Exception as error:
                on_error(error)
        return clicked

    for amount, _ in SUPPORT_OPTIONS:
        window.On["LookBridgeSupport{}".format(amount)].Clicked = callback(
            lambda amount=amount: open_support(amount)
        )
    window.On["LookBridgeUpdates"].Clicked = callback(check_for_updates)
