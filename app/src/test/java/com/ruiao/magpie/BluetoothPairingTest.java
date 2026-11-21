package com.ruiao.magpie;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class BluetoothPairingTest {
    private static final String SERVICE = "3354a0e7-0681-40bb-add0-fd9e286f1d70";
    private static final String FINGERPRINT = "0123456789abcdef".repeat(4);
    private static final String TOKEN = "A".repeat(43);
    private static final String QR = "magpie-ble:1:" + SERVICE + ":" + FINGERPRINT + ":" + TOKEN;

    @Test public void acceptsDesktopQrAndKeepsCertificatePin() {
        BluetoothPairing pairing = BluetoothPairing.parse(QR);
        assertEquals(SERVICE, pairing.serviceId.toString());
        assertEquals(FINGERPRINT + ":" + TOKEN, pairing.code);
        assertEquals(TOKEN, pairing.token());
        assertTrue(pairing.sameAs(BluetoothPairing.parse(pairing.encode())));
    }

    @Test public void rejectsNonMagpieCodesAndMalformedComponents() {
        for (String invalid : new String[]{
                "https://example.com", QR + "\n", QR.replace(":1:", ":9:"), QR.replace(SERVICE, "0-0-0-0-0"),
                QR.replace(FINGERPRINT, "g" + FINGERPRINT.substring(1)), QR.substring(0, QR.length() - 1) + "="
        }) {
            assertThrows(IllegalArgumentException.class, () -> BluetoothPairing.parse(invalid));
        }
    }

    @Test public void unsyncedPairingExpiresBeforeDesktopQrTtl() {
        assertEquals(45_000L, BluetoothPairing.RECORD_TTL_MS);
        assertFalse(BluetoothPairing.recordExpired(0, 44_999L));
        assertTrue(BluetoothPairing.recordExpired(0, 45_000L));
    }

    @Test public void startupRetriesUseBoundedExponentialDelays() {
        assertEquals(3, BluetoothPairing.MAX_STARTUP_RETRIES);
        assertEquals(1_000L, BluetoothPairing.startupRetryDelay(1));
        assertEquals(2_000L, BluetoothPairing.startupRetryDelay(2));
        assertEquals(4_000L, BluetoothPairing.startupRetryDelay(3));
        assertEquals(4_000L, BluetoothPairing.startupRetryDelay(99));
    }
}
