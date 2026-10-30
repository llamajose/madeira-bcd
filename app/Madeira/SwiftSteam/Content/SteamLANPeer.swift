// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 llamajose
// Madeira Converter Exception: see LICENSE-EXCEPTION.md
//
// Steam's local network game transfers (docs/STEAM_LIBRARY.md, "From a PC on
// this network"): chunks of a depot come from Valve's client on a PC in the
// same network that has the game installed, instead of from a content server.
// The message types and fields are Valve's (steammessages_remoteclient_discovery,
// steammessages_contentsystem and steammessages_clientlanp2p protobufs).
//
// 1. Discovery: one CMsgRemoteClientBroadcastDiscovery sent straight to the
//    PC's UDP port 27036 (no broadcast: iOS gives sideloaded apps no multicast
//    entitlement). The PC answers with its status: its remote client ID, host
//    name and signed-in users.
// 2. ContentServerDirectory.RequestPeerContentServer (DepotDownloader, over the
//    app's CM connection) asks Steam to open that PC's content server for the
//    app: a TCP port, the depots it has and an access token.
// 3. Chunks: k_EMsgClientPeerChunkRequest / k_EMsgClientPeerChunkResponse on
//    that TCP connection, framed like every Steam TCP message (length, "VT01",
//    message type with the protobuf flag, header length 0, body).

import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Settings › Steam › "PC on this network": the IPv4 address of a PC whose
/// Steam client should supply downloads, and this device's own client ID for
/// Steam's local network messages (random, kept for the install).
enum SteamLANSetting {
    static let hostKey = "madeira.steam.lanPeerHost"
    static let clientIDKey = "madeira.steam.lanClientID"

    /// The PC's address, "" when the feature is off.
    static var host: String {
        get { (UserDefaults.standard.string(forKey: hostKey) ?? "").trimmingCharacters(in: .whitespaces) }
        set {
            let value = newValue.trimmingCharacters(in: .whitespaces)
            if value.isEmpty { UserDefaults.standard.removeObject(forKey: hostKey) }
            else { UserDefaults.standard.set(value, forKey: hostKey) }
        }
    }

    static let peerClientIDKey = "madeira.steam.lanPeerClientID"

    /// The PC's Steam client ID: entered by hand (Steam's logs/remote_connections.txt,
    /// "Loaded client id") or kept from its last discovery answer. 0 = unknown.
    static var peerClientID: UInt64 {
        get { UInt64(UserDefaults.standard.string(forKey: peerClientIDKey) ?? "") ?? 0 }
        set {
            if newValue == 0 { UserDefaults.standard.removeObject(forKey: peerClientIDKey) }
            else { UserDefaults.standard.set(String(newValue), forKey: peerClientIDKey) }
        }
    }

    static func isValidAddress(_ text: String) -> Bool {
        var address = in_addr()
        return inet_pton(AF_INET, text, &address) == 1
    }

    /// Never 0 and stable across launches (Steam keys the PC's permission to it).
    static var localClientID: UInt64 {
        if let stored = UserDefaults.standard.string(forKey: clientIDKey), let id = UInt64(stored), id != 0 { return id }
        var id: UInt64 = 0
        while id == 0 { id = UInt64.random(in: 1...UInt64.max) }
        UserDefaults.standard.set(String(id), forKey: clientIDKey)
        return id
    }
}

/// What a PC's Steam client says about itself on the local network.
struct SteamLANPeerStatus: Sendable, Equatable {
    var clientID: UInt64
    var hostname: String
    var users: [UInt64]
}

enum SteamLANPeer {
    static let discoveryPort: UInt16 = 27036
    /// 0xFFFFFFFFA05F4C21, little-endian, at the start of every discovery datagram.
    /// On the wire FF FF FF FF 21 4C 5F A0, as a PC's Steam client sends its
    /// status (captured on llama-pc, 2026-10-29); the reversed order is ignored.
    static let discoveryMagic: [UInt8] = [0xFF, 0xFF, 0xFF, 0xFF, 0x21, 0x4C, 0x5F, 0xA0]
    static let peerChunkRequest: UInt32 = 9902   // k_EMsgClientPeerChunkRequest
    static let peerChunkResponse: UInt32 = 9903  // k_EMsgClientPeerChunkResponse
    static let tcpMagic: [UInt8] = Array("VT01".utf8)

    // MARK: Discovery datagrams

    /// A discovery request from `clientID` (ERemoteClientBroadcastMsg 0).
    static func discoveryPacket(clientID: UInt64, sequence: UInt32) -> Data {
        var header = ProtobufEncoder()
        header.writeUInt64(fieldNumber: 1, value: clientID)   // client_id
        header.writeTag(fieldNumber: 2, wireType: .varint)     // msg_type = Discovery (0), written explicitly
        header.writeVarint(0)
        var body = ProtobufEncoder()
        body.writeUInt32(fieldNumber: 1, value: sequence)      // seq_num
        var packet = Data(discoveryMagic)
        append32(&packet, UInt32(header.data.count)); packet.append(header.data)
        append32(&packet, UInt32(body.data.count)); packet.append(body.data)
        return packet
    }

    /// The status a Steam client answers with (ERemoteClientBroadcastMsg 1), or nil.
    static func parseStatus(_ packet: Data) -> SteamLANPeerStatus? {
        let bytes = [UInt8](packet)
        guard bytes.count >= 16, Array(bytes[0..<8]) == discoveryMagic else { return nil }
        let headerLength = Int(read32(bytes, 8))
        guard headerLength >= 0, 12 + headerLength + 4 <= bytes.count else { return nil }
        let header = Data(bytes[12..<(12 + headerLength)])
        let bodyLength = Int(read32(bytes, 12 + headerLength))
        let bodyStart = 16 + headerLength
        guard bodyLength >= 0, bodyStart + bodyLength <= bytes.count else { return nil }
        let body = Data(bytes[bodyStart..<(bodyStart + bodyLength)])

        var clientID: UInt64 = 0
        var type: UInt64 = 0
        var decoder = ProtobufDecoder(header)
        while let tag = try? decoder.readTag() {
            switch tag.fieldNumber {
            case 1: clientID = (try? decoder.readVarint()) ?? 0
            case 2: type = (try? decoder.readVarint()) ?? 0
            default: guard (try? decoder.skip(wireType: tag.wireType)) != nil else { return nil }
            }
        }
        guard type == 1, clientID != 0 else { return nil }

        var status = SteamLANPeerStatus(clientID: clientID, hostname: "", users: [])
        var fields = ProtobufDecoder(body)
        while let tag = try? fields.readTag() {
            switch tag.fieldNumber {
            case 4: status.hostname = (try? fields.readString()) ?? ""
            case 9:
                guard let user = try? fields.readBytes() else { return status }
                var u = ProtobufDecoder(user)
                while let t = try? u.readTag() {
                    if t.fieldNumber == 1, t.wireType == .fixed64, let id = try? u.readFixed64() { status.users.append(id) }
                    else if (try? u.skip(wireType: t.wireType)) == nil { break }
                }
            default: guard (try? fields.skip(wireType: tag.wireType)) != nil else { return status }
            }
        }
        return status
    }

    /// What one discovery round saw, for the log.
    struct DiscoveryReport: Sendable {
        var status: SteamLANPeerStatus?
        /// Where the status came from (the PC may answer from another of its addresses).
        var from = ""
        var note = ""
    }

    /// Ask the Steam client at `host` (an IPv4 address) for its status. Blocking;
    /// call it off the main actor. Tries a few times, `timeout` seconds each.
    static func discover(host: String, clientID: UInt64, attempts: Int = 3, timeout: TimeInterval = 1.5) -> DiscoveryReport {
        var report = DiscoveryReport()
        var target = sockaddr_in()
        target.sin_family = sa_family_t(AF_INET)
        target.sin_port = discoveryPort.bigEndian
        guard inet_pton(AF_INET, host, &target.sin_addr) == 1 else { report.note = "bad address"; return report }

        #if canImport(Darwin)
        let fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        #else
        let fd = socket(AF_INET, Int32(SOCK_DGRAM.rawValue), Int32(IPPROTO_UDP))
        #endif
        guard fd >= 0 else { report.note = "socket errno=\(errno)"; return report }
        defer { close(fd) }
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        // The answer may go to the discovery port or to the sender's port:
        // listen on 27036 when it is free, else on any port.
        var local = sockaddr_in()
        local.sin_family = sa_family_t(AF_INET)
        local.sin_port = discoveryPort.bigEndian
        local.sin_addr.s_addr = INADDR_ANY
        let bound = withUnsafePointer(to: &local) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        var boundPort = Int(discoveryPort)
        if bound != 0 {
            local.sin_port = 0
            _ = withUnsafePointer(to: &local) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
            }
            boundPort = 0
        }
        setTimeout(fd, timeout)

        var sent = 0, sendErrno: Int32 = 0, datagrams = 0, others: [String] = []
        for attempt in 0..<max(1, attempts) {
            let packet = [UInt8](discoveryPacket(clientID: clientID, sequence: UInt32(attempt + 1)))
            let rc = withUnsafePointer(to: &target) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    sendto(fd, packet, packet.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
            if rc == packet.count { sent += 1 } else { sendErrno = errno }
            let deadline = Date().addingTimeInterval(timeout)
            var buffer = [UInt8](repeating: 0, count: 2048)
            while Date() < deadline {
                var from = sockaddr_in()
                var fromLength = socklen_t(MemoryLayout<sockaddr_in>.size)
                let n = withUnsafeMutablePointer(to: &from) {
                    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { recvfrom(fd, &buffer, buffer.count, 0, $0, &fromLength) }
                }
                guard n > 0 else { break }  // timeout
                datagrams += 1
                var text = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                var addr = from.sin_addr
                inet_ntop(AF_INET, &addr, &text, socklen_t(INET_ADDRSTRLEN))
                let source = String(cString: text)
                // Any Steam client's status counts: the PC may answer from
                // another of its addresses, or by broadcast.
                if let status = parseStatus(Data(buffer[0..<n])), status.clientID != clientID {
                    report.status = status
                    report.from = source
                    report.note = "sent=\(sent) received=\(datagrams) bound=\(boundPort)"
                    return report
                }
                if others.count < 3 { others.append("\(source):\(n)") }
            }
        }
        report.note = "sent=\(sent)/\(max(1, attempts)) send-errno=\(sendErrno) received=\(datagrams) bound=\(boundPort)" +
            (others.isEmpty ? "" : " other=\(others.joined(separator: ","))")
        return report
    }

    // MARK: RequestPeerContentServer

    /// What Steam answers when it opened a PC's content server.
    struct PeerServer: Sendable, Equatable {
        var port: UInt16 = 0
        var depots: Set<UInt32> = []
        var accessToken: UInt64 = 0
    }

    /// CContentServerDirectory_RequestPeerContentServer_Request.
    static func peerServerRequest(localClientID: UInt64, steamID: UInt64, serverClientID: UInt64,
                                  appID: UInt32, buildID: UInt32) -> Data {
        var body = ProtobufEncoder()
        body.writeUInt64(fieldNumber: 1, value: localClientID)    // remote_client_id
        body.writeUInt64(fieldNumber: 2, value: steamID)          // steamid
        body.writeUInt64(fieldNumber: 3, value: serverClientID)   // server_remote_client_id
        body.writeUInt32(fieldNumber: 4, value: appID)            // app_id
        body.writeUInt32(fieldNumber: 5, value: buildID)          // current_build_id
        return body.data
    }

    /// CContentServerDirectory_RequestPeerContentServer_Response. A port of 0
    /// or no token means Steam did not open the PC's content server.
    static func parsePeerServerResponse(_ data: Data) throws -> PeerServer {
        var server = PeerServer()
        var decoder = ProtobufDecoder(data)
        while let tag = try decoder.readTag() {
            switch (tag.fieldNumber, tag.wireType) {
            case (1, .varint): server.port = UInt16(truncatingIfNeeded: try decoder.readVarint())
            case (2, .varint): server.depots.insert(UInt32(truncatingIfNeeded: try decoder.readVarint()))
            case (2, .lengthDelimited):   // packed repeated uint32
                var packed = ProtobufDecoder(try decoder.readBytes())
                while !packed.isAtEnd { server.depots.insert(UInt32(truncatingIfNeeded: try packed.readVarint())) }
            case (3, .varint): server.accessToken = try decoder.readVarint()
            case (3, .fixed64): server.accessToken = try decoder.readFixed64()
            default: try decoder.skip(wireType: tag.wireType)
            }
        }
        return server
    }

    /// CContentServerDirectory_GetPeerContentInfo_Request.
    static func peerContentInfoRequest(localClientID: UInt64, steamID: UInt64, serverClientID: UInt64) -> Data {
        var body = ProtobufEncoder()
        body.writeUInt64(fieldNumber: 1, value: localClientID)    // remote_client_id
        body.writeUInt64(fieldNumber: 2, value: steamID)          // steamid
        body.writeUInt64(fieldNumber: 3, value: serverClientID)   // server_remote_client_id
        return body.data
    }

    /// CContentServerDirectory_GetPeerContentInfo_Response: the apps the PC
    /// serves and its public IP.
    static func parsePeerContentInfo(_ data: Data) throws -> (apps: Set<UInt32>, publicIP: String) {
        var apps = Set<UInt32>(), ip = ""
        var decoder = ProtobufDecoder(data)
        while let tag = try decoder.readTag() {
            switch (tag.fieldNumber, tag.wireType) {
            case (1, .varint): apps.insert(UInt32(truncatingIfNeeded: try decoder.readVarint()))
            case (1, .lengthDelimited):
                var packed = ProtobufDecoder(try decoder.readBytes())
                while !packed.isAtEnd { apps.insert(UInt32(truncatingIfNeeded: try packed.readVarint())) }
            case (2, .lengthDelimited): ip = try decoder.readString()
            default: try decoder.skip(wireType: tag.wireType)
            }
        }
        return (apps, ip)
    }

    /// One line for a RemoteClientSteamClient notification Steam pushed to
    /// this device: who sent it and, for a relayed discovery packet, what it says.
    static func describeRelayed(method: String, body: Data) -> String {
        var steamID: UInt64 = 0, clientID: UInt64 = 0, payload = Data()
        var decoder = ProtobufDecoder(body)
        fields: while let tag = try? decoder.readTag() {
            switch (tag.fieldNumber, tag.wireType) {
            case (1, .fixed64): steamID = (try? decoder.readFixed64()) ?? 0
            case (2, .fixed64): clientID = (try? decoder.readFixed64()) ?? 0
            case (3, .lengthDelimited): payload = (try? decoder.readBytes()) ?? Data()
            default: if (try? decoder.skip(wireType: tag.wireType)) == nil { break fields }
            }
        }
        let name = method.replacingOccurrences(of: "RemoteClientSteamClient.", with: "")
        var line = "\(name) bytes=\(body.count) from-client=\(clientID) same-account=\(steamID == 0 ? "?" : "1") payload=\(payload.count)"
        if payload.count >= 16, Array(payload.prefix(8)) == discoveryMagic {
            let headerLength = Int(read32([UInt8](payload), 8))
            var type: UInt64 = 99
            if headerLength >= 0, 12 + headerLength <= payload.count {
                var h = ProtobufDecoder(payload.subdata(in: payload.startIndex + 12 ..< payload.startIndex + 12 + headerLength))
                while let t = try? h.readTag() {
                    if t.fieldNumber == 2, t.wireType == .varint { type = (try? h.readVarint()) ?? 99 }
                    else if (try? h.skip(wireType: t.wireType)) == nil { break }
                }
            }
            line += " discovery-type=\(type)"
            if let status = parseStatus(payload) { line += " status host=\(status.hostname) client=\(status.clientID)" }
        } else if !payload.isEmpty {
            line += " head=" + payload.prefix(48).map { String(format: "%02x", $0) }.joined()
        }
        return line
    }

    // MARK: RemoteClient service

    /// CRemoteClient_Online_Notification: this device as a remote client of `steamID`.
    static func onlineNotification(steamID: UInt64, localClientID: UInt64, name: String) -> Data {
        var details = ProtobufEncoder()
        details.writeString(fieldNumber: 1, value: name)        // device_friendly_name
        details.writeInt32(fieldNumber: 2, value: -102)         // os_type: macOS, as at logon
        details.writeUInt32(fieldNumber: 3, value: 1)           // gaming_device_type: standard PC
        var body = ProtobufEncoder()
        body.writeFixed64(fieldNumber: 1, value: steamID)       // steamid
        body.writeFixed64(fieldNumber: 2, value: localClientID) // remote_client_id
        body.writeSubmessage(fieldNumber: 3, value: details.data)
        return body.data
    }

    struct RecentClient: Sendable, Equatable {
        var clientID: UInt64 = 0
        var name = ""
        var online = false
    }

    /// CRemoteClient_GetRecentClients_Response.
    static func parseRecentClients(_ data: Data) throws -> [RecentClient] {
        var clients: [RecentClient] = []
        var decoder = ProtobufDecoder(data)
        while let tag = try decoder.readTag() {
            guard tag.fieldNumber == 1, tag.wireType == .lengthDelimited else { try decoder.skip(wireType: tag.wireType); continue }
            var client = RecentClient()
            var c = ProtobufDecoder(try decoder.readBytes())
            while let t = try c.readTag() {
                switch (t.fieldNumber, t.wireType) {
                case (1, .fixed64): client.clientID = try c.readFixed64()
                case (1, .varint): client.clientID = try c.readVarint()
                case (2, .lengthDelimited):
                    var d = ProtobufDecoder(try c.readBytes())
                    while let dt = try d.readTag() {
                        if dt.fieldNumber == 1, dt.wireType == .lengthDelimited { client.name = try d.readString() }
                        else { try d.skip(wireType: dt.wireType) }
                    }
                case (8, .varint): client.online = try c.readVarint() != 0
                default: try c.skip(wireType: t.wireType)
                }
            }
            clients.append(client)
        }
        return clients
    }

    // MARK: TCP framing

    /// One Steam TCP frame around a protobuf message with an empty header.
    static func frame(eMsg: UInt32, body: Data) -> Data {
        var payload = Data()
        append32(&payload, eMsg | 0x8000_0000)
        append32(&payload, 0)
        payload.append(body)
        var out = Data()
        append32(&out, UInt32(payload.count))
        out.append(contentsOf: tcpMagic)
        out.append(payload)
        return out
    }

    static func chunkRequest(appID: UInt32, depotID: UInt32, sha: Data, accessToken: UInt64) -> Data {
        var body = ProtobufEncoder()
        body.writeUInt32(fieldNumber: 1, value: appID)
        body.writeUInt32(fieldNumber: 2, value: depotID)
        body.writeBytes(fieldNumber: 3, value: sha)
        body.writeUInt64(fieldNumber: 4, value: accessToken)
        return frame(eMsg: peerChunkRequest, body: body.data)
    }

    struct ChunkResponse: Sendable {
        var result: UInt32 = 0
        var depotID: UInt32 = 0
        var sha = Data()
        var encrypted = false
        var compressed = false
        var data = Data()
    }

    /// The body of a k_EMsgClientPeerChunkResponse (after the frame and the message header).
    static func parseChunkResponse(_ body: Data) throws -> ChunkResponse {
        var response = ChunkResponse()
        var decoder = ProtobufDecoder(body)
        while let tag = try decoder.readTag() {
            switch tag.fieldNumber {
            case 1: response.result = UInt32(truncatingIfNeeded: try decoder.readVarint())
            case 3: response.depotID = UInt32(truncatingIfNeeded: try decoder.readVarint())
            case 4: response.sha = try decoder.readBytes()
            case 5: response.encrypted = try decoder.readVarint() != 0
            case 6: response.compressed = try decoder.readVarint() != 0
            case 7: response.data = try decoder.readBytes()
            default: try decoder.skip(wireType: tag.wireType)
            }
        }
        return response
    }

    // MARK: Helpers

    static func append32(_ data: inout Data, _ value: UInt32) {
        withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
    }

    static func read32(_ bytes: [UInt8], _ offset: Int) -> UInt32 {
        UInt32(bytes[offset]) | UInt32(bytes[offset + 1]) << 8 | UInt32(bytes[offset + 2]) << 16 | UInt32(bytes[offset + 3]) << 24
    }

    static func setTimeout(_ fd: Int32, _ seconds: TimeInterval) {
        var tv = timeval()
        tv.tv_sec = Int(seconds)
        tv.tv_usec = .init((seconds - floor(seconds)) * 1_000_000)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    }
}

/// One PC's content server for one app, as RequestPeerContentServer opened it.
/// Requests go one at a time over a single TCP connection on a private serial
/// queue, so the download tasks never block a thread of their own. After a few
/// failures in a row the source stops being used and every chunk goes to the
/// content servers.
final class SteamPeerChunkSource: @unchecked Sendable {
    let host: String
    let port: UInt16
    let appID: UInt32
    let depots: Set<UInt32>
    private let accessToken: UInt64
    private let queue = DispatchQueue(label: "madeira.steam.lan-peer")
    private let lock = NSLock()
    private var fd: Int32 = -1
    private var failuresInARow = 0
    private var disabled = false
    private(set) var chunks = 0
    private(set) var bytes: UInt64 = 0
    private(set) var failures = 0
    static let maximumFailuresInARow = 4

    init(host: String, port: UInt16, appID: UInt32, depots: Set<UInt32>, accessToken: UInt64) {
        self.host = host; self.port = port; self.appID = appID; self.depots = depots; self.accessToken = accessToken
    }

    var usable: Bool { lock.lock(); defer { lock.unlock() }; return !disabled }

    func serves(_ depotID: UInt32) -> Bool { usable && (depots.isEmpty || depots.contains(depotID)) }

    /// The chunk's bytes as the PC sends them (see `ChunkResponse.encrypted` / `.compressed`).
    func fetch(depotID: UInt32, sha: Data) async throws -> SteamLANPeer.ChunkResponse {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<SteamLANPeer.ChunkResponse, Error>) in
            queue.async {
                do { continuation.resume(returning: try self.fetchBlocking(depotID: depotID, sha: sha)) }
                catch { continuation.resume(throwing: error) }
            }
        }
    }

    func recordSuccess(bytes count: Int) {
        lock.lock(); chunks += 1; bytes += UInt64(count); failuresInARow = 0; lock.unlock()
    }

    /// Returns true when this failure turned the source off.
    @discardableResult func recordFailure() -> Bool {
        lock.lock(); defer { lock.unlock() }
        failures += 1; failuresInARow += 1
        if !disabled, failuresInARow >= Self.maximumFailuresInARow { disabled = true; return true }
        return false
    }

    func close() { queue.sync { self.disconnect() } }

    // MARK: On the queue

    private func fetchBlocking(depotID: UInt32, sha: Data) throws -> SteamLANPeer.ChunkResponse {
        guard usable else { throw SteamError.chunkDownloadFailed("The PC on this network is no longer used.") }
        if fd < 0 { try connect() }
        let request = [UInt8](SteamLANPeer.chunkRequest(appID: appID, depotID: depotID, sha: sha, accessToken: accessToken))
        do {
            try sendAll(request)
            while true {
                let header = try receive(8)
                guard Array(header[4..<8]) == SteamLANPeer.tcpMagic else { throw SteamError.chunkDownloadFailed("Unexpected data from the PC.") }
                let length = Int(SteamLANPeer.read32(header, 0))
                guard length >= 8, length <= ContentDecryptor.maximumChunkBytes + 4096 else {
                    throw SteamError.chunkDownloadFailed("Unexpected message size from the PC.")
                }
                let payload = try receive(length)
                let eMsg = SteamLANPeer.read32(payload, 0) & 0x7FFF_FFFF
                let headerLength = Int(SteamLANPeer.read32(payload, 4))
                guard 8 + headerLength <= payload.count else { throw SteamError.chunkDownloadFailed("Unexpected message from the PC.") }
                guard eMsg == SteamLANPeer.peerChunkResponse else { continue }
                let response = try SteamLANPeer.parseChunkResponse(Data(payload[(8 + headerLength)...]))
                guard response.sha == sha else { continue }  // an answer to an earlier, abandoned request
                return response
            }
        } catch {
            disconnect()
            throw error
        }
    }

    private func connect() throws {
        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        guard inet_pton(AF_INET, host, &address.sin_addr) == 1 else { throw SteamError.chunkDownloadFailed("Bad PC address.") }
        #if canImport(Darwin)
        let s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)
        #else
        let s = socket(AF_INET, Int32(SOCK_STREAM.rawValue), Int32(IPPROTO_TCP))
        #endif
        guard s >= 0 else { throw SteamError.chunkDownloadFailed("No socket (errno \(errno)).") }
        var yes: Int32 = 1
        setsockopt(s, Int32(IPPROTO_TCP), TCP_NODELAY, &yes, socklen_t(MemoryLayout<Int32>.size))
        #if canImport(Darwin)
        setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &yes, socklen_t(MemoryLayout<Int32>.size))
        #endif
        SteamLANPeer.setTimeout(s, 15)
        let rc = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Glibc_or_Darwin_connect(s, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        guard rc == 0 else {
            let code = errno
            Darwin_or_Glibc_close(s)
            throw SteamError.chunkDownloadFailed("Cannot reach the PC (errno \(code)).")
        }
        fd = s
    }

    private func disconnect() {
        if fd >= 0 { Darwin_or_Glibc_close(fd); fd = -1 }
    }

    private func sendAll(_ bytes: [UInt8]) throws {
        var sent = 0
        while sent < bytes.count {
            let n = bytes.withUnsafeBytes { raw -> Int in
                #if canImport(Darwin)
                return send(fd, raw.baseAddress! + sent, raw.count - sent, 0)
                #else
                return send(fd, raw.baseAddress! + sent, raw.count - sent, Int32(MSG_NOSIGNAL))
                #endif
            }
            guard n > 0 else { throw SteamError.chunkDownloadFailed("The PC closed the connection (errno \(errno)).") }
            sent += n
        }
    }

    private func receive(_ count: Int) throws -> [UInt8] {
        var buffer = [UInt8](repeating: 0, count: count)
        var got = 0
        while got < count {
            let n = buffer.withUnsafeMutableBytes { recv(fd, $0.baseAddress! + got, count - got, 0) }
            guard n > 0 else { throw SteamError.chunkDownloadFailed("The PC closed the connection (errno \(errno)).") }
            got += n
        }
        return buffer
    }
}

// `connect` and `close` are also method names in this file; these reach the C functions.
@inline(__always) private func Glibc_or_Darwin_connect(_ fd: Int32, _ address: UnsafePointer<sockaddr>, _ length: socklen_t) -> Int32 {
    connect(fd, address, length)
}
@inline(__always) private func Darwin_or_Glibc_close(_ fd: Int32) { _ = close(fd) }
