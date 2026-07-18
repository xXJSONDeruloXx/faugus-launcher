"""GTK manager for CPUID Fault Emulation and UwU hosted-game activation."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from faugus.hv_client import helper_command, helper_path, inspect
from faugus.path_manager import GAMES_JSON
from faugus.utils import destroy_and_release, hide_dialog_action_area, load_json_file

OS_NAMES = {"bazzite": "Bazzite", "steamos": "SteamOS", "linux": "Linux"}


def enabled_game_count():
    games = load_json_file(GAMES_JSON, default=[])
    return sum(
        1 for game in games
        if isinstance(game, dict)
        and game.get("runner") != "Steam"
        and game.get("hv_enabled", True) is not False
    )


class OperationDialog(Gtk.Dialog):
    def __init__(self, parent, title, command, finished):
        super().__init__(title=title, transient_for=parent)
        hide_dialog_action_area(self)
        self.set_modal(True)
        self.set_default_size(640, 440)
        self.set_resizable(True)
        self.finished_callback = finished
        self.running = True

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(16)
        root.set_margin_bottom(12)
        root.set_margin_start(16)
        root.set_margin_end(16)

        heading = Gtk.Box(spacing=12)
        self.spinner = Gtk.Spinner(spinning=True)
        self.spinner.set_size_request(28, 28)
        heading.append(self.spinner)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.title_label = Gtk.Label(label=title, xalign=0)
        self.title_label.add_css_class("title-2")
        self.summary = Gtk.Label(label="This may take a few minutes. Details are shown below.", xalign=0)
        self.summary.add_css_class("dim-label")
        labels.append(self.title_label)
        labels.append(self.summary)
        heading.append(labels)
        root.append(heading)

        self.output = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.output.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.output.set_left_margin(10)
        self.output.set_right_margin(10)
        self.output.set_top_margin(8)
        self.output.set_bottom_margin(8)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.output)
        scroll.add_css_class("card")
        root.append(scroll)

        self.close_button = Gtk.Button(label="Done", sensitive=False)
        self.close_button.connect("clicked", lambda *_: self.response(Gtk.ResponseType.CLOSE))
        self.close_button.set_hexpand(True)
        root.append(self.close_button)
        self.get_content_area().append(root)
        self.connect("response", lambda dialog, _response: destroy_and_release(dialog))
        self.connect("close-request", lambda *_: self.running)

        threading.Thread(target=self._run, args=(command,), daemon=True).start()

    def append(self, text):
        buffer = self.output.get_buffer()
        buffer.insert(buffer.get_end_iter(), text)
        mark = buffer.create_mark(None, buffer.get_end_iter(), False)
        self.output.scroll_mark_onscreen(mark)
        return False

    def _run(self, command):
        output = []
        try:
            process = subprocess.Popen(
                command, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=1,
            )
            for line in process.stdout:
                output.append(line)
                GLib.idle_add(self.append, line)
            code = process.wait()
        except Exception as error:
            output.append(f"{error}\n")
            GLib.idle_add(self.append, output[-1])
            code = 1
        GLib.idle_add(self.finish, code, "".join(output))

    def finish(self, code, output):
        self.running = False
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.close_button.set_sensitive(True)
        if code == 0:
            self.title_label.set_text("Completed")
            self.summary.set_text("The operation completed successfully.")
            self.close_button.add_css_class("suggested-action")
        else:
            self.title_label.set_text("Something went wrong")
            self.summary.set_text("Review the output, then try again.")
            self.close_button.add_css_class("destructive-action")
        self.finished_callback(code, output)
        return False


class CompatibilityManager(Gtk.Dialog):
    def __init__(self, parent):
        super().__init__(title="CPUID Compatibility", transient_for=parent)
        hide_dialog_action_area(self)
        self.set_modal(True)
        self.set_default_size(700, 680)
        self.set_resizable(True)
        self.closed = False
        self.state = None
        self.probe_result = None

        css = Gtk.CssProvider()
        css.load_from_string("""
            .hv-summary { padding: 18px; border-radius: 12px; }
            .hv-orb { padding: 13px; border-radius: 999px; }
            .hv-good { color: @success_color; background: alpha(@success_color, .14); }
            .hv-warn { color: @warning_color; background: alpha(@warning_color, .14); }
            .hv-bad { color: @error_color; background: alpha(@error_color, .14); }
            .hv-value { font-weight: 600; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toolbar = Gtk.Box(spacing=8)
        toolbar.set_margin_top(10)
        toolbar.set_margin_bottom(10)
        toolbar.set_margin_start(12)
        toolbar.set_margin_end(12)
        title = Gtk.Label(label="CPUID Compatibility", xalign=0, hexpand=True)
        title.add_css_class("title-2")
        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh status")
        self.refresh_button.connect("clicked", lambda *_: self.refresh())
        toolbar.append(title)
        toolbar.append(self.refresh_button)
        outer.append(toolbar)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_top(4)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        scroll.set_child(content)
        outer.append(scroll)

        summary = Gtk.Box(spacing=14)
        summary.add_css_class("card")
        summary.add_css_class("hv-summary")
        self.orb = Gtk.Box()
        self.orb.add_css_class("hv-orb")
        self.summary_icon = Gtk.Image(icon_name="content-loading-symbolic", pixel_size=26)
        self.orb.append(self.summary_icon)
        summary.append(self.orb)
        summary_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.summary_title = Gtk.Label(label="Checking your system…", xalign=0)
        self.summary_title.add_css_class("title-3")
        self.summary_subtitle = Gtk.Label(label="Reading module and runtime state.", xalign=0, wrap=True)
        self.summary_subtitle.add_css_class("dim-label")
        summary_text.append(self.summary_title)
        summary_text.append(self.summary_subtitle)
        summary.append(summary_text)
        content.append(summary)

        status_frame, status_grid = self.section("Status")
        self.status_values = {}
        for row, (key, label) in enumerate((
            ("installed", "Installed"),
            ("current", "Current kernel"),
            ("runtime", "Runtime"),
            ("automatic", "Automatic game use"),
        )):
            name = Gtk.Label(label=label, xalign=0, hexpand=True)
            value = Gtk.Label(label="Checking…", xalign=1)
            value.add_css_class("hv-value")
            status_grid.attach(name, 0, row, 1, 1)
            status_grid.attach(value, 1, row, 1, 1)
            self.status_values[key] = value
        content.append(status_frame)

        diagnostic_frame, diagnostic_grid = self.section("System and diagnostics")
        self.system_value = self.action_row(diagnostic_grid, 0, "Running kernel", "Checking…")
        self.probe_value, self.test_button = self.action_row(
            diagnostic_grid, 1, "CPUID faulting", "Not tested", "Test"
        )
        self.test_button.connect("clicked", lambda *_: self.run_probe(show_output=True))
        self.umip_value, self.umip_button = self.action_row(
            diagnostic_grid, 2, "UMIP boot option", "Checking…", "Configure…"
        )
        self.umip_button.connect("clicked", self.on_umip_clicked)
        content.append(diagnostic_frame)

        module_frame, module_grid = self.section("Module controls")
        self.module_value, self.module_button = self.action_row(
            module_grid, 0, "Kernel module", "Checking…", "Install…"
        )
        self.module_button.connect("clicked", self.on_module_clicked)
        self.manual_value, self.manual_button = self.action_row(
            module_grid, 1, "Manual runtime", "Checking…", "Start"
        )
        self.manual_button.connect("clicked", self.on_manual_clicked)
        content.append(module_frame)

        automatic_frame, automatic_grid = self.section("Automatic hosted-game activation")
        self.automatic_value, self.automatic_button = self.action_row(
            automatic_grid, 0, "UwU game leases", "Checking…", "Enable…"
        )
        self.automatic_button.connect("clicked", self.on_automatic_clicked)
        note = Gtk.Label(
            label=("UwU starts CPUID compatibility before each enabled hosted game and stops it "
                   "after the last enabled game exits. New hosted games default to enabled."),
            xalign=0, wrap=True,
        )
        note.add_css_class("dim-label")
        automatic_grid.attach(note, 0, 1, 3, 1)
        content.append(automatic_frame)

        maintenance_frame, maintenance_grid = self.section("Maintenance")
        _, remove = self.action_row(
            maintenance_grid, 0, "Remove CPUID Fault Emulation",
            "Stops automatic activation and removes the module", "Remove…"
        )
        remove.add_css_class("destructive-action")
        remove.connect("clicked", self.on_remove_clicked)
        content.append(maintenance_frame)

        close = Gtk.Button(label="Close")
        close.set_margin_top(10)
        close.set_margin_bottom(10)
        close.set_margin_start(12)
        close.set_margin_end(12)
        close.connect("clicked", lambda *_: self.response(Gtk.ResponseType.CLOSE))
        outer.append(close)

        self.get_content_area().append(outer)
        self.connect("response", self.on_response)
        self.refresh()
        self.run_probe(show_output=False)

    @staticmethod
    def section(title):
        frame = Gtk.Frame(label=title)
        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        frame.set_child(grid)
        return frame, grid

    @staticmethod
    def action_row(grid, row, title, subtitle, action=None):
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title_label = Gtk.Label(label=title, xalign=0)
        subtitle_label = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        subtitle_label.add_css_class("dim-label")
        labels.append(title_label)
        labels.append(subtitle_label)
        grid.attach(labels, 0, row, 1, 1)
        if action is None:
            return subtitle_label
        button = Gtk.Button(label=action, valign=Gtk.Align.CENTER)
        grid.attach(button, 1, row, 1, 1)
        return subtitle_label, button

    def on_response(self, dialog, _response):
        self.closed = True
        destroy_and_release(dialog)

    def refresh(self):
        self.refresh_button.set_sensitive(False)

        def worker():
            try:
                state, error = inspect(), None
            except Exception as exc:
                state, error = None, str(exc)
            GLib.idle_add(self.apply_state, state, error)

        threading.Thread(target=worker, daemon=True).start()

    def apply_state(self, state, error):
        if self.closed:
            return False
        self.refresh_button.set_sensitive(True)
        if error:
            self.summary_title.set_text("Status unavailable")
            self.summary_subtitle.set_text(error)
            self.summary_icon.set_from_icon_name("dialog-error-symbolic")
            self.orb.set_css_classes(["hv-orb", "hv-bad"])
            return False

        self.state = state
        os_name = OS_NAMES.get(state["os"], state["os"].title())
        self.system_value.set_text(f"{os_name} • {state['kernel']} • {state['arch']}")
        count = enabled_game_count()

        if not state["installed"]:
            heading, detail, icon, style = (
                "Setup required",
                "Install CPUID Fault Emulation before launching enabled hosted games.",
                "application-x-firmware-symbolic", "hv-warn",
            )
        elif not state["matching"]:
            heading, detail, icon, style = (
                "Update required",
                "Rebuild the module for the running kernel before launching enabled games.",
                "software-update-urgent-symbolic", "hv-bad",
            )
        elif state["leases"]:
            heading, detail, icon, style = (
                f"Active for {state['leases']} game{'s' if state['leases'] != 1 else ''}",
                "Compatibility is held until the last enabled hosted game exits.",
                "media-playback-start-symbolic", "hv-good",
            )
        elif not state["runtime_active"]:
            heading, detail, icon, style = (
                "Automatic activation is off",
                "Enable the UwU runtime before launching games that require compatibility.",
                "dialog-warning-symbolic", "hv-warn",
            )
        else:
            heading, detail, icon, style = (
                "Ready for enabled games",
                "Compatibility is idle and will activate before a hosted game starts.",
                "object-select-symbolic", "hv-good",
            )

        self.summary_title.set_text(heading)
        self.summary_subtitle.set_text(f"{detail}  •  {os_name}")
        self.summary_icon.set_from_icon_name(icon)
        self.orb.set_css_classes(["hv-orb", style])

        self.status_values["installed"].set_text("Installed" if state["installed"] else "Not installed")
        self.status_values["current"].set_text(
            "Current" if state["matching"] else "Update required" if state["installed"] else "Unavailable"
        )
        if state["loaded"]:
            runtime_text = "Running automatically" if state["runtime_owns_module"] else "Running manually"
        else:
            runtime_text = "Stopped · idle"
        self.status_values["runtime"].set_text(runtime_text)
        auto_text = (
            f"Active · {state['leases']} game(s)"
            if state["leases"] else
            f"Ready · {count} game(s)" if state["runtime_active"] else "Disabled"
        )
        self.status_values["automatic"].set_text(auto_text)

        self.module_value.set_text(
            "Installed for the running kernel" if state["matching"] else
            "Rebuild required" if state["installed"] else
            "Not installed"
        )
        self.module_button.set_label(
            "Update…" if state["installed"] and not state["matching"] else
            "Rebuild…" if state["installed"] else "Install…"
        )
        self.module_button.set_sensitive(state["leases"] == 0)

        self.manual_value.set_text(runtime_text)
        self.manual_button.set_label("Stop" if state["loaded"] else "Start")
        self.manual_button.set_sensitive(state["installed"] and state["matching"] and state["leases"] == 0)

        automatic_ready = state["runtime_enabled"] and state["runtime_active"]
        self.automatic_value.set_text(
            f"Enabled • {count} opted-in hosted game(s)" if automatic_ready else
            "Disabled • game selections are retained"
        )
        self.automatic_button.set_label("Disable…" if automatic_ready else "Enable…")
        self.automatic_button.set_sensitive(state["installed"] and state["matching"])

        disabled = state["umip"] == "disabled"
        configured = state["umip_arg"] == "present"
        if configured:
            umip_text = "Disabled via clearcpuid=514" if disabled else "Restart required to disable UMIP"
        else:
            umip_text = "Enabled • no override" if not disabled else "Disabled by the running system"
        self.umip_value.set_text(umip_text)
        self.umip_button.set_label("Restore…" if configured else "Disable…")
        return False

    def run_probe(self, show_output):
        self.probe_value.set_text("Running the isolated diagnostic…")
        self.test_button.set_sensitive(False)

        def worker():
            result = subprocess.run(
                ["/usr/bin/python3", str(helper_path()), "--cpuid-probe"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            GLib.idle_add(self.apply_probe, result.returncode, result.stdout, show_output)

        threading.Thread(target=worker, daemon=True).start()

    def apply_probe(self, code, output, show_output):
        self.test_button.set_sensitive(True)
        loaded = bool(self.state and self.state.get("loaded"))
        if code == 0:
            text = "End-to-end bypass works" if loaded else "Native CPUID faulting detected"
        elif code == 2:
            text = "Emulation module required"
        elif code == 3:
            text = "CPUID faulting test failed"
        elif code == 4:
            text = "Unsupported architecture"
        else:
            text = f"Diagnostic failed with status {code}"
        self.probe_value.set_text(text)
        self.probe_result = (code, output)
        if show_output:
            self.show_text_dialog(text, output.strip() or "No diagnostic output was produced.")
        return False

    def show_text_dialog(self, heading, body):
        dialog = Gtk.Dialog(title=heading, transient_for=self, modal=True)
        hide_dialog_action_area(dialog)
        dialog.set_default_size(560, 280)
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(body)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(view)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: dialog.response(Gtk.ResponseType.CLOSE))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12); box.set_margin_bottom(12); box.set_margin_start(12); box.set_margin_end(12)
        box.append(scroll); box.append(close)
        dialog.get_content_area().append(box)
        dialog.connect("response", lambda d, _r: destroy_and_release(d))
        dialog.present()

    def confirm(self, heading, body, accept, callback, destructive=False):
        dialog = Gtk.Dialog(title=heading, transient_for=self, modal=True)
        hide_dialog_action_area(dialog)
        label = Gtk.Label(label=body, wrap=True, max_width_chars=58, xalign=0)
        cancel = Gtk.Button(label="Cancel", hexpand=True)
        proceed = Gtk.Button(label=accept, hexpand=True)
        proceed.add_css_class("destructive-action" if destructive else "suggested-action")
        buttons = Gtk.Box(spacing=8, homogeneous=True)
        buttons.append(cancel); buttons.append(proceed)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(18); box.set_margin_bottom(12); box.set_margin_start(18); box.set_margin_end(18)
        box.append(label); box.append(buttons)
        dialog.get_content_area().append(box)
        cancel.connect("clicked", lambda *_: dialog.response(Gtk.ResponseType.CANCEL))
        proceed.connect("clicked", lambda *_: dialog.response(Gtk.ResponseType.OK))
        def response(d, response_id):
            destroy_and_release(d)
            if response_id == Gtk.ResponseType.OK:
                callback()
        dialog.connect("response", response)
        dialog.present()

    def execute(self, action, title, values=()):
        operation = OperationDialog(
            self, title, helper_command(action, privileged=True, values=values),
            lambda _code, _output: self.refresh(),
        )
        operation.present()

    def on_module_clicked(self, *_):
        action = "update" if self.state and self.state["installed"] else "install"
        local = self.state and self.state["os"] in {"bazzite", "steamos"}
        body = (
            "A kernel-matched module will be compiled in Podman. The build image uses about 1–2 GB."
            if local else
            "Build tools and kernel headers will be installed if needed, then the module will be managed with DKMS."
        )
        self.confirm(
            "Rebuild CPUID Fault Emulation?" if action == "update" else "Install CPUID Fault Emulation?",
            body, "Update" if action == "update" else "Install",
            lambda: self.execute(action, "Updating the kernel module" if action == "update" else "Installing the kernel module"),
        )

    def on_manual_clicked(self, *_):
        action = "stop" if self.state and self.state["loaded"] else "start"
        self.execute(action, f"{action.title()}ing CPUID compatibility")

    def on_automatic_clicked(self, *_):
        enabled = self.state and self.state["runtime_enabled"] and self.state["runtime_active"]
        action = "disable_runtime" if enabled else "enable_runtime"
        body = (
            "UwU will stop acquiring the module automatically. Existing per-game selections are retained."
            if enabled else
            "UwU will acquire the module before opted-in hosted games start and release it after the last one exits. "
            "Any legacy Steam-log watcher will be disabled and its previous state remembered."
        )
        self.confirm(
            "Disable automatic activation?" if enabled else "Enable automatic activation?",
            body, "Disable" if enabled else "Enable",
            lambda: self.execute(action, "Disabling automatic activation" if enabled else "Enabling automatic activation"),
            destructive=bool(enabled),
        )

    def on_umip_clicked(self, *_):
        configured = self.state and self.state["umip_arg"] == "present"
        action = "enable_umip" if configured else "disable_umip"
        verb = "Restore" if configured else "Disable"
        self.confirm(
            f"{verb} UMIP?",
            ("This removes clearcpuid=514 from boot options." if configured else
             "This applies clearcpuid=514 to boot options for affected games.") + " A restart is required.",
            verb, lambda: self.execute(action, "Updating boot options"),
        )

    def on_remove_clicked(self, *_):
        self.confirm(
            "Remove CPUID Fault Emulation?",
            "The module will be stopped, automatic activation disabled, and the DKMS/local module removed.",
            "Remove", lambda: self.execute("uninstall", "Removing CPUID Fault Emulation"),
            destructive=True,
        )
