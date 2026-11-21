package com.ruiao.magpie;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;

import java.nio.charset.StandardCharsets;
import java.util.Locale;

import org.junit.Test;

public class BluetoothFramesTest {
    @Test public void frameUsesOneByteOperationAndBigEndianStreamId() {
        assertArrayEquals(new byte[]{16, (byte) 0xab, (byte) 0xcd, 2},
                BluetoothTunnel.encode(16, 0xabcd, new byte[]{2}));
    }

    @Test public void authenticationMatchesDesktopHmacVectors() {
        byte[] key = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx".getBytes(StandardCharsets.US_ASCII);
        byte[] host = new byte[16];
        byte[] phone = new byte[16];
        for (int i = 0; i < host.length; i++) {
            host[i] = (byte) i;
            phone[i] = (byte) (i + 16);
        }
        assertEquals("5d87288c32692d68a8628571e55ddffa", hex(BluetoothTunnel.proof(key, host, phone, "phone")));
        assertEquals("ab93fd9e1bd3a3c6bf512e9cf98fe40f", hex(BluetoothTunnel.proof(key, host, phone, "host")));
    }

    private static String hex(byte[] bytes) {
        StringBuilder text = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) text.append(String.format(Locale.ROOT, "%02x", value & 255));
        return text.toString();
    }
}
