import Foundation
import SwiftUI

@main
struct MadeiraApp: App {
    init() {
        // madeira-bcd: the Metal Performance HUD's performance insights (the
        // notes that stack up left of its panel: shader compiles, similar render
        // passes, blit encoders) cover the game. Off unless madeira.cfg says
        // env.MTL_HUD_INSIGHTS_ENABLED = 1, which is exported later and wins.
        setenv("MTL_HUD_INSIGHTS_ENABLED", "0", 0)
        // ml1172: read the screen on the main thread; library entries, whose
        // default Resolution comes from it, are also made on other threads.
        _ = ResolutionChoices.screen
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .modifier(ClaimGamepadEvents())
                .onAppear {
                    GamepadInput.shared.start()
                    HardwareInput.shared.start()
                    JITNetworkShortcut.shared.restoreLeftover()   // also starts its network path monitor
                }
                // madeira://jit-network/... (the Madeira JIT shortcut returning, JITNetwork.swift),
                // else madeira://play?exe=... (Home Screen shortcuts, SavesAndShortcuts.swift).
                .onOpenURL { url in if !JITNetworkShortcut.shared.handle(url) { ShortcutRouter.shared.handle(url) } }
        }
    }
}

/// Identifies which build is installed: logged at launch and shown under
/// Setup Guide > About.
enum BuildInfo {

    /// When the app code was linked: the newer modification time of the executable
    /// and, in Debug builds, Madeira.debug.dylib (Xcode may leave the stub alone).
    static let builtAt: String = {
        var paths = [Bundle.main.executablePath].compactMap { $0 }
        paths.append(Bundle.main.bundlePath + "/Madeira.debug.dylib")
        let dates = paths.compactMap {
            (try? FileManager.default.attributesOfItem(atPath: $0))?[.modificationDate] as? Date
        }
        guard let date = dates.max() else { return "unknown" }
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f.string(from: date)
    }()

    static var summary: String { "built \(builtAt)" }
}
