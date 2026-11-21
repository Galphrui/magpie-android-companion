package com.ruiao.magpie;

import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** The sole on-camera pairing format accepted by the companion. */
final class BluetoothPairing {
    static final String ORIGIN = "https://127.0.0.1:43435";
    static final String BROWSER_ORIGIN = "https://127.0.0.1:43437";
    static final long RECORD_TTL_MS = 45_000L;
    static final int MAX_STARTUP_RETRIES = 3;
    private static final String PREFIX = "magpie-ble:1:";
    private static final Pattern FORMAT = Pattern.compile(PREFIX
            + "([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}):([0-9a-f]{64}):([A-Za-z0-9_-]{43})");

    final UUID serviceId;
    final String code;

    private BluetoothPairing(UUID serviceId, String fingerprint, String token) {
        this.serviceId = serviceId;
        this.code = fingerprint + ":" + token;
    }

    static BluetoothPairing parse(String data) {
        if (data == null || data.length() != 158) throw new IllegalArgumentException("Invalid Bluetooth QR code");
        Matcher match = FORMAT.matcher(data);
        if (!match.matches()) throw new IllegalArgumentException("Invalid Bluetooth QR code");
        return new BluetoothPairing(UUID.fromString(match.group(1)), match.group(2), match.group(3));
    }

    String token() { return code.substring(65); }

    String encode() { return PREFIX + serviceId + ":" + code; }

    boolean sameAs(BluetoothPairing other) {
        return other != null && serviceId.equals(other.serviceId) && code.equals(other.code);
    }

    static boolean recordExpired(long startedAt, long now) {
        return now - startedAt >= RECORD_TTL_MS;
    }

    static long startupRetryDelay(int attempt) {
        return 1000L << Math.max(0, Math.min(2, attempt - 1));
    }
}
