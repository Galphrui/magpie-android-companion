package com.ruiao.magpie;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.Binder;
import android.os.Bundle;

/** One-shot pairing handoff accepted only from Android's authorized ADB shell UID. */
public final class UsbPairingProvider extends ContentProvider {
    static final String ACTION_USB_PAIR_UPDATED = ".USB_PAIR_UPDATED";
    static final String USB_ORIGIN = "https://127.0.0.1:43425";
    private static final int ADB_SHELL_UID = 2000;

    @Override public boolean onCreate() { return true; }

    @Override public Bundle call(String method, String arg, Bundle extras) {
        if (Binder.getCallingUid() != ADB_SHELL_UID) throw new SecurityException("USB handoff requires an authorized ADB shell");
        if (!"pair".equals(method) || getContext() == null || arg == null) throw new IllegalArgumentException("Unsupported USB pairing call");
        try {
            // The local app stores the short-lived credential encrypted with Android Keystore.
            new BridgeClient(arg, USB_ORIGIN);
            PairingStore.consumeUsb(getContext(), arg);
            Bundle result = new Bundle(); result.putString("result", "paired"); return result;
        } catch (SecurityException | IllegalArgumentException e) { throw e; }
        catch (Exception e) { throw new IllegalStateException("Could not securely import the USB pairing", e); }
    }

    @Override public String getType(Uri uri) { return null; }
    @Override public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) { throw new UnsupportedOperationException("Read-only USB handoff"); }
    @Override public Uri insert(Uri uri, ContentValues values) { throw new UnsupportedOperationException("Use the one-shot pair call"); }
    @Override public int delete(Uri uri, String selection, String[] selectionArgs) { throw new UnsupportedOperationException("Read-only USB handoff"); }
    @Override public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) { throw new UnsupportedOperationException("Read-only USB handoff"); }
}
