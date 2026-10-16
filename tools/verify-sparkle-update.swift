#!/usr/bin/env swift
import CryptoKit
import Foundation

// Public-key-only verification of a release's signed appcast and installer.
// Sparkle 2.10 signs the exact bytes before the final signature comment:
// https://github.com/sparkle-project/Sparkle/blob/2.10.0/Sparkle/SPUExtractSignedFeed.m
// https://github.com/sparkle-project/Sparkle/blob/2.10.0/common_cli/Signing.swift
// Never reserialize the XML before checking its signature.

struct VerificationFailure: Error, LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

func require(_ condition: Bool, _ message: String) throws {
    if !condition { throw VerificationFailure(message: message) }
}

func signature(_ value: String?, description: String) throws -> Data {
    guard let value, let bytes = Data(base64Encoded: value), bytes.count == 64 else {
        throw VerificationFailure(message: "\(description) must be a 64-byte Ed25519 signature in Base64.")
    }
    return bytes
}

func signedFeedContent(_ data: Data, key: Curve25519.Signing.PublicKey) throws -> Data {
    let prefix = Data("<!-- sparkle-signatures:\n".utf8)
    let suffix = Data("-->".utf8)
    guard let start = data.range(of: prefix, options: .backwards),
          let end = data.range(of: suffix, in: start.upperBound..<data.endIndex),
          let block = String(data: data[start.upperBound..<end.lowerBound], encoding: .utf8) else {
        throw VerificationFailure(message: "The appcast has no valid Sparkle signature trailer.")
    }
    // Sparkle's generator appends a newline. Reject any unsigned content after
    // the trailer, and ambiguous metadata, rather than silently ignoring it.
    try require(data[end.upperBound...].allSatisfy { [9, 10, 13, 32].contains($0) },
                "The appcast contains unsigned data after its signature trailer.")
    var fields: [String: String] = [:]
    for line in block.split(separator: "\n") {
        let parts = line.split(separator: ":", maxSplits: 1)
        try require(parts.count == 2, "Malformed appcast signature metadata.")
        let name = String(parts[0])
        try require((name == "edSignature" || name == "length") && fields[name] == nil,
                    "Unexpected or duplicate appcast signature metadata.")
        fields[name] = parts[1].trimmingCharacters(in: .whitespacesAndNewlines)
    }
    let content = Data(data[..<start.lowerBound])
    try require(fields["length"].flatMap(UInt64.init) == UInt64(content.count),
                "The appcast's signed-content length does not match its bytes.")
    let signed = try signature(fields["edSignature"], description: "The appcast signature")
    try require(key.isValidSignature(signed, for: content), "The appcast signature is invalid.")
    return content
}

func verify(publicKey: String, appcast: URL, archive: URL) throws {
    guard let bytes = Data(base64Encoded: publicKey), bytes.count == 32 else {
        throw VerificationFailure(message: "The public key must contain 32 bytes in Base64.")
    }
    let key = try Curve25519.Signing.PublicKey(rawRepresentation: bytes)
    let feed = try Data(contentsOf: appcast)
    let content = try signedFeedContent(feed, key: key)
    // Parse only authenticated content, without allowing external entities.
    let document = try XMLDocument(data: content, options: .nodeLoadExternalEntitiesNever)
    let enclosures = try document.nodes(forXPath: "/rss/channel/item/enclosure")
    try require(enclosures.count == 1, "Expected exactly one full installer enclosure in this release's appcast.")
    guard let enclosure = enclosures.first as? XMLElement else {
        throw VerificationFailure(message: "The appcast has no installer enclosure.")
    }
    let namespace = "http://www.andymatuschak.org/xml-namespaces/sparkle"
    let archiveSignature = try signature(
        enclosure.attribute(forLocalName: "edSignature", uri: namespace)?.stringValue,
        description: "The installer signature"
    )
    let expectedLength = enclosure.attribute(forName: "length")?.stringValue.flatMap(UInt64.init)
    let archiveBytes = try Data(contentsOf: archive, options: .mappedIfSafe)
    try require(expectedLength == UInt64(archiveBytes.count),
                "The installer length does not match the appcast enclosure.")
    try require(key.isValidSignature(archiveSignature, for: archiveBytes), "The installer signature is invalid.")
}

do {
    try require(CommandLine.arguments.count == 4,
                "Usage: xcrun swift tools/verify-sparkle-update.swift PUBLIC_KEY_BASE64 APPCAST_PATH ARCHIVE_PATH")
    try verify(publicKey: CommandLine.arguments[1],
               appcast: URL(fileURLWithPath: CommandLine.arguments[2]),
               archive: URL(fileURLWithPath: CommandLine.arguments[3]))
    print("Verified appcast and installer signatures and lengths using only the public key.")
} catch {
    FileHandle.standardError.write(Data("Verification failed: \(error.localizedDescription)\n".utf8))
    exit(1)
}
