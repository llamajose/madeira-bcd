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
                // madeira://jit-network/...: the Madeira JIT shortcut returning (JITNetwork.swift);
                // madeira-bcd: madeira://play?exe=... (Home Screen shortcuts).
                .onOpenURL { url in
                    if url.host == "jit-network" { JITNetworkShortcut.shared.handle(url) }
                    else { ShortcutRouter.shared.handle(url) }
                }
        }
    }
}
