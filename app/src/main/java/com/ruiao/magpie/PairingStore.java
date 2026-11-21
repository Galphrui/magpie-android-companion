package com.ruiao.magpie;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import java.util.Base64;

/** Stores the pairing token encrypted at rest with a non-exportable Android Keystore key. */
final class PairingStore {
    private static final String ALIAS = "magpie.companion.pairing.v1";
    private static final String PREFS = "companion_pairing";
    private static final String BLUETOOTH = "bluetooth_pairing";

    static final class Pairing {
        final String code, origin;
        Pairing(String code, String origin) { this.code = code; this.origin = origin; }
    }

    private static synchronized SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (store.containsAlias(ALIAS)) return (SecretKey) store.getKey(ALIAS, null);
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .setKeySize(256)
                .build());
        return generator.generateKey();
    }

    private static byte[] seal(byte[] plain) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        byte[] encrypted = cipher.doFinal(plain);
        byte[] iv = cipher.getIV();
        byte[] result = new byte[1 + iv.length + encrypted.length];
        result[0] = (byte) iv.length;
        System.arraycopy(iv, 0, result, 1, iv.length);
        System.arraycopy(encrypted, 0, result, 1 + iv.length, encrypted.length);
        return result;
    }

    private static byte[] open(byte[] sealed) throws Exception {
        if (sealed.length < 30) throw new IllegalArgumentException("Invalid encrypted pairing");
        int ivLength = sealed[0] & 0xff;
        if (ivLength < 12 || ivLength > 16 || sealed.length <= 1 + ivLength + 16) {
            throw new IllegalArgumentException("Invalid encrypted pairing");
        }
        byte[] iv = new byte[ivLength];
        byte[] ciphertext = new byte[sealed.length - 1 - ivLength];
        System.arraycopy(sealed, 1, iv, 0, ivLength);
        System.arraycopy(sealed, 1 + ivLength, ciphertext, 0, ciphertext.length);
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, iv));
        return cipher.doFinal(ciphertext);
    }

    static Pairing load(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String value = prefs.getString("pairing", null);
        String origin = prefs.getString("origin", "https://127.0.0.1:43425");
        if (value == null) return null;
        try {
            String code = new String(open(Base64.getDecoder().decode(value)), StandardCharsets.UTF_8);
            new BridgeClient(code, origin); // Fail closed if the persisted address or pair is invalid.
            return new Pairing(code, origin);
        } catch (Exception e) {
            clear(context);
            return null;
        }
    }

    static void save(Context context, String code, String origin) throws Exception {
        String encrypted = Base64.getEncoder().encodeToString(seal(code.getBytes(StandardCharsets.UTF_8)));
        if (!context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString("pairing", encrypted).putString("origin", origin).commit()) {
            throw new IllegalStateException("Could not securely save pairing");
        }
    }

    static void consumeUsb(Context context, String code) throws Exception {
        save(context, code, UsbPairingProvider.USB_ORIGIN);
    }

    static void clear(Context context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().remove("pairing").remove("origin").commit();
    }

    static BluetoothPairing loadBluetooth(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String value = prefs.getString(BLUETOOTH, null);
        if (value == null) return null;
        try {
            return BluetoothPairing.parse(new String(open(Base64.getDecoder().decode(value)), StandardCharsets.UTF_8));
        } catch (Exception ignored) {
            clearBluetooth(context);
            return null;
        }
    }

    static void saveBluetooth(Context context, BluetoothPairing pairing) throws Exception {
        String encrypted = Base64.getEncoder().encodeToString(seal(pairing.encode().getBytes(StandardCharsets.UTF_8)));
        if (!context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(BLUETOOTH, encrypted).commit()) {
            throw new IllegalStateException("Could not securely save Bluetooth pairing");
        }
    }

    static void clearBluetooth(Context context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().remove(BLUETOOTH).commit();
    }
    private PairingStore() { }
}
