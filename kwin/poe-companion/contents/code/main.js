function report(window) {
    if (!window) {
        callDBus("org.poecompanion.Companion", "/org/poecompanion/Companion", "org.poecompanion.Companion", "FocusChanged", "", "", "", false);
        return;
    }
    var output = window.output ? window.output.name : "";
    var appId = window.resourceClass || window.resourceName || window.desktopFileName || "";
    callDBus("org.poecompanion.Companion", "/org/poecompanion/Companion", "org.poecompanion.Companion", "FocusChanged", String(appId), String(window.caption || ""), String(output), true);
}

workspace.windowActivated.connect(report);
report(workspace.activeWindow);

// Keep the app's focus-loss watchdog informed even while one game window stays active.
var focusHeartbeat = new QTimer();
focusHeartbeat.interval = 2000;
focusHeartbeat.timeout.connect(function () { report(workspace.activeWindow); });
focusHeartbeat.start();
