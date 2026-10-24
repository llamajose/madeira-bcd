//
//  LibraryPrefs.swift
//  Madeira
//
//  madeira-bcd: per-title choices that are not command-line arguments: a
//  game's own launch switches, kept by its Windows path (AVX, Report an NVIDIA
//  GPU, Wine's C++ runtime ...), which every launch of the game exports
//  (BCDLaunch, LibraryBCD.swift), and the madeira-bcd home screen's per-game
//  choices (primary program, last played, screen size). Moved here from
//  HomeView.swift so the library does not depend on the fork-only home screen.
//

import Foundation
import UIKit

/// Per-title choices that are not command-line arguments (those stay in
/// GameArguments, keyed by Windows path, exactly as the old library kept them).
enum LibraryPrefs {
    private static let primaryKey = "madeira.library.primaryExe"
    private static let playedKey = "madeira.library.lastPlayed"
    private static let desktopKey = "madeira.library.inDesktop"
    private static let avxKey = "madeira.library.avx"
    private static let vcrtKey = "madeira.library.wineVCRT"
    private static let nvidiaKey = "madeira.library.nvidia"
    private static let safeSyncKey = "madeira.library.safeSync"
    private static let screenKey = "madeira.library.screen"
    /// Per-game virtual monitor sizes; "" = default (1024x768), "fill" = the
    /// device's own landscape shape at 720 lines (experimental), "fill-mfx15" =
    /// the same shape at 480 lines, which MetalFX 1.5x brings back to "fill".
    static let screenSizes = ["", "1280x720", "fill", "fill-mfx15", "1600x900", "1920x1080"]

    static func screenLabel(_ value: String) -> String {
        switch value {
        case "": return "Default (1024x768)"
        case "fill":
            let s = screenPixels(value)
            return "Fill the screen, \(s.0)x\(s.1) (experimental)"
        case "fill-mfx15":
            let s = screenPixels(value), f = screenPixels("fill")
            return "Fill with MetalFX 1.5×, \(s.0)x\(s.1) → \(f.0)x\(f.1) (experimental)"
        default: return value
        }
    }

    /// "fill": 720 lines at the panel's aspect (iPhone 17 Pro Max 2868x1320 ->
    /// 1564x720), so the picture needs no pillarbox. Width kept even.
    static func screenPixels(_ value: String) -> (Int, Int) {
        if value == "fill-mfx15" {
            // The fill width / 1.5 at 480 lines, even: 1564x720 -> 1042x480,
            // which MetalFX's 1.5x scale turns back into (about) 1564x720.
            let f = screenPixels("fill")
            return (max(640, Int((Double(f.0) / 1.5 / 2).rounded(.down)) * 2), 480)
        }
        guard value == "fill" else { return ContentView.desktopSize(value) }
        let n = UIScreen.main.nativeBounds.size
        let long = max(n.width, n.height), short = min(n.width, n.height)
        guard short > 0 else { return (1280, 720) }
        let w = Int((720 * long / short / 2).rounded()) * 2
        return (max(960, min(w, 1920)), 720)
    }

    private static func dict<T>(_ key: String) -> [String: T] {
        (UserDefaults.standard.dictionary(forKey: key) as? [String: T]) ?? [:]
    }

    private static func store<T>(_ value: T?, _ key: String, _ field: String) {
        var all: [String: T] = dict(key)
        all[field] = value
        UserDefaults.standard.set(all, forKey: key)
    }

    static func primaryPath(for title: String) -> String? { (dict(primaryKey) as [String: String])[title] }
    static func setPrimaryPath(_ path: String, for title: String) { store(path, primaryKey, title) }

    static func lastPlayed(_ title: String) -> Date? {
        guard let t = (dict(playedKey) as [String: Double])[title] else { return nil }
        return Date(timeIntervalSince1970: t)
    }
    static func markPlayed(_ title: String) { store(Date().timeIntervalSince1970, playedKey, title) }

    static func inDesktop(_ windowsPath: String) -> Bool { (dict(desktopKey) as [String: Bool])[windowsPath] ?? false }
    static func setInDesktop(_ on: Bool, for windowsPath: String) { store(on ? true : nil, desktopKey, windowsPath) }

    static func avx(_ windowsPath: String) -> Bool { (dict(avxKey) as [String: Bool])[windowsPath] ?? false }
    static func setAVX(_ on: Bool, for windowsPath: String) { store(on ? true : nil, avxKey, windowsPath) }

    static func wineVCRT(_ windowsPath: String) -> Bool { (dict(vcrtKey) as [String: Bool])[windowsPath] ?? false }
    static func setWineVCRT(_ on: Bool, for windowsPath: String) { store(on ? true : nil, vcrtKey, windowsPath) }

    static func nvidia(_ windowsPath: String) -> Bool { (dict(nvidiaKey) as [String: Bool])[windowsPath] ?? false }
    static func setNvidia(_ on: Bool, for windowsPath: String) { store(on ? true : nil, nvidiaKey, windowsPath) }

    static func safeSync(_ windowsPath: String) -> Bool { (dict(safeSyncKey) as [String: Bool])[windowsPath] ?? false }
    static func setSafeSync(_ on: Bool, for windowsPath: String) { store(on ? true : nil, safeSyncKey, windowsPath) }

    static func screen(_ windowsPath: String) -> String { (dict(screenKey) as [String: String])[windowsPath] ?? "" }
    static func setScreen(_ size: String, for windowsPath: String) { store(size.isEmpty ? nil : size, screenKey, windowsPath) }

    /// FNV-1a over a string: stable across launches, unlike String.hashValue.
    /// Names a game's config file (GameProfiles.swift) and fingerprints the
    /// recommended settings Madeira wrote (GameRecommendationsApply.swift).
    static func stableHash(_ s: String) -> UInt64 {
        var h: UInt64 = 0xcbf29ce484222325
        for b in s.utf8 { h = (h ^ UInt64(b)) &* 0x100000001b3 }
        return h
    }
}
