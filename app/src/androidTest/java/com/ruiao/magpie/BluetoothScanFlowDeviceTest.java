package com.ruiao.magpie;

import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Rect;
import android.os.SystemClock;
import android.os.ParcelFileDescriptor;
import android.view.View;
import android.view.ViewGroup;
import android.widget.TextView;

import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.concurrent.FutureTask;
import javax.net.ssl.HttpsURLConnection;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.Assume;

/** Exercises QR handoff, expiry, and live synchronization on a real Android device. */
@RunWith(AndroidJUnit4.class)
public final class BluetoothScanFlowDeviceTest {
    private static final int SCAN_BLUETOOTH = 516;
    private static final String SERVICE = "3354a0e7-0681-40bb-add0-fd9e286f1d70";
    private static final String FINGERPRINT = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    private static final String TOKEN = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
    private static final String TEST_QR = "magpie-ble:1:" + SERVICE + ":" + FINGERPRINT + ":" + TOKEN;

    @Test public void scannedQrStartsTunnelAndExpiresWithoutCrash() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        PairingStore.clearBluetooth(context);

        MainActivity activity = startActivity();
        try {
            BluetoothPairing pairing = BluetoothPairing.parse(TEST_QR);
            injectScannedPairing(activity, pairing);

            BluetoothTunnel tunnel = waitForTunnel(activity, 5000);
            assertNotNull("scanning the QR must create a BLE tunnel", tunnel);
            assertTrue("BLE GATT server must be registered on the device",
                    waitForGattRegistration(pairing, 5000));

            setLongField(activity, "bluetoothPairingStartedAt",
                    SystemClock.elapsedRealtime() - BluetoothPairing.RECORD_TTL_MS - 1);
            InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> invokeStartBluetooth(activity));

            assertNull("expired QR must clear the pending record", getField(activity, "pendingBluetooth"));
            assertNull("expired QR must stop the old tunnel", getField(activity, "bluetoothTunnel"));
            assertNull("expired QR must not leave saved credentials", PairingStore.loadBluetooth(context));
            String status = (String) getField(activity, "bluetoothStatus");
            assertTrue(status.contains("过期"));
        } finally {
            InstrumentationRegistry.getInstrumentation().runOnMainSync(activity::finish);
        }
    }

    @Test public void liveDesktopQrStartsBleServiceOnDevice() throws Exception {
        String qr = qrArgument();
        Assume.assumeTrue("pass the current desktop QR with runner argument qr", qr != null);
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        PairingStore.clearBluetooth(context);

        BluetoothPairing pairing = BluetoothPairing.parse(qr);
        MainActivity activity = startActivity();
        try {
            injectScannedPairing(activity, pairing);
            BluetoothTunnel tunnel = waitForTunnel(activity, 5000);
            assertNotNull("the current desktop QR must start a BLE tunnel", tunnel);
            assertTrue("phone GATT service must be registered and advertised",
                    waitForGattRegistration(pairing, 5000));
            assertDesktopPaired(activity, 37_000);
            assertSyncedAgentRendered(activity);
            assertDesktopSettings(pairing);
        } finally {
            InstrumentationRegistry.getInstrumentation().runOnMainSync(activity::finish);
        }
    }

    private static String qrArgument() {
        String value = InstrumentationRegistry.getArguments().getString("qr");
        return value == null || value.trim().isEmpty() || "true".equalsIgnoreCase(value.trim())
                ? null : value.trim();
    }

    private static void injectScannedPairing(MainActivity activity, BluetoothPairing pairing) throws Exception {
        setStaticResult(pairing);
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> invokeActivityResult(activity));
    }

    private static MainActivity startActivity() {
        Intent intent = new Intent(InstrumentationRegistry.getInstrumentation().getTargetContext(), MainActivity.class);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return (MainActivity) InstrumentationRegistry.getInstrumentation().startActivitySync(intent);
    }

    private static BluetoothTunnel waitForTunnel(MainActivity activity, long timeoutMs) throws Exception {
        long deadline = SystemClock.uptimeMillis() + timeoutMs;
        Object value;
        do {
            value = getField(activity, "bluetoothTunnel");
            if (value != null) return (BluetoothTunnel) value;
            SystemClock.sleep(100);
        } while (SystemClock.uptimeMillis() < deadline);
        return null;
    }

    private static boolean waitForGattRegistration(BluetoothPairing pairing, long timeoutMs) throws Exception {
        long deadline = SystemClock.uptimeMillis() + timeoutMs;
        String packageName = InstrumentationRegistry.getInstrumentation().getTargetContext().getPackageName();
        String uuid = pairing.serviceId.toString().toLowerCase();
        while (SystemClock.uptimeMillis() < deadline) {
            ParcelFileDescriptor descriptor = InstrumentationRegistry.getInstrumentation().getUiAutomation()
                    .executeShellCommand("dumpsys bluetooth_manager");
            try (InputStream input = new ParcelFileDescriptor.AutoCloseInputStream(descriptor)) {
                ByteArrayOutputStream output = new ByteArrayOutputStream();
                byte[] buffer = new byte[4096];
                int length;
                while ((length = input.read(buffer)) >= 0) output.write(buffer, 0, length);
                String dump = new String(output.toByteArray(), StandardCharsets.UTF_8).toLowerCase();
                if (dump.contains("gatt server map") && dump.contains(packageName.toLowerCase())
                        && dump.contains(uuid)) return true;
            }
            SystemClock.sleep(200);
        }
        return false;
    }

    private static boolean waitForPaired(MainActivity activity, long timeoutMs) throws Exception {
        long deadline = SystemClock.uptimeMillis() + timeoutMs;
        while (SystemClock.uptimeMillis() < deadline) {
            Object tunnel = getField(activity, "bluetoothTunnel");
            Object status = getField(activity, "bluetoothStatus");
            if (tunnel instanceof BluetoothTunnel && ((BluetoothTunnel) tunnel).isPaired()) return true;
            if ("蓝牙已连接电脑".equals(status)) return true;
            SystemClock.sleep(250);
        }
        return false;
    }

    private static void assertDesktopPaired(MainActivity activity, long timeoutMs) throws Exception {
        if (waitForPaired(activity, timeoutMs)) return;
        assertTrue("desktop did not authenticate the current QR; phone status="
                        + getField(activity, "bluetoothStatus") + ", tunnel="
                        + describeTunnel(getField(activity, "bluetoothTunnel")),
                false);
    }

    private static void assertSyncedAgentRendered(MainActivity activity) throws Exception {
        long deadline = SystemClock.uptimeMillis() + 30_000;
        while (SystemClock.uptimeMillis() < deadline) {
            FutureTask<Boolean> rendered = new FutureTask<>(() -> {
                JSONObject snapshot = (JSONObject) getField(activity, "snapshot");
                if (snapshot == null || !snapshot.optBoolean("upstreamAvailable")) return false;
                JSONArray agents = snapshot.getJSONArray("agents");
                if (agents.length() == 0) return false;
                JSONObject agent = agents.getJSONObject(0);
                String name = agent.optString("name", agent.getString("id"));
                return hasVisibleText(activity.findViewById(android.R.id.content), name);
            });
            InstrumentationRegistry.getInstrumentation().runOnMainSync(rendered);
            if (rendered.get()) return;
            SystemClock.sleep(250);
        }
        assertTrue("native UI did not render an agent from the live desktop snapshot; phone status="
                + getField(activity, "status"), false);
    }

    private static boolean hasVisibleText(View view, String expected) {
        if (view instanceof TextView && expected.contentEquals(((TextView) view).getText())) {
            return view.isShown() && view.getGlobalVisibleRect(new Rect());
        }
        if (view instanceof ViewGroup) {
            ViewGroup group = (ViewGroup) view;
            for (int index = 0; index < group.getChildCount(); index++) {
                if (hasVisibleText(group.getChildAt(index), expected)) return true;
            }
        }
        return false;
    }

    private static void assertDesktopSettings(BluetoothPairing pairing) throws Exception {
        BridgeClient bridge = new BridgeClient(pairing.code, BluetoothPairing.ORIGIN);
        String cookie = bridge.desktopCookie().split(";", 2)[0];
        Method openUrl = BridgeClient.class.getDeclaredMethod("openUrl", String.class, String.class);
        openUrl.setAccessible(true);
        HttpsURLConnection connection = (HttpsURLConnection) openUrl.invoke(bridge,
                BluetoothPairing.BROWSER_ORIGIN + "/api/settings", "application/json");
        connection.setReadTimeout(20_000);
        connection.setRequestProperty("Cookie", cookie);
        try {
            assertTrue("desktop settings must be returned through BLE", connection.getResponseCode() == 200);
            try (InputStream input = connection.getInputStream()) {
                assertTrue("desktop settings must contain a response body", input.read() != -1);
            }
        } finally {
            connection.disconnect();
        }
    }

    private static String describeTunnel(Object value) {
        if (!(value instanceof BluetoothTunnel)) return String.valueOf(value);
        BluetoothTunnel tunnel = (BluetoothTunnel) value;
        return "closed=" + tunnel.isClosed() + ", paired=" + tunnel.isPaired();
    }

    private static void setStaticResult(BluetoothPairing pairing) throws Exception {
        Field result = BluetoothScanActivity.class.getDeclaredField("result");
        result.setAccessible(true);
        result.set(null, pairing);
    }

    private static void invokeActivityResult(MainActivity activity) {
        try {
            Method method = MainActivity.class.getDeclaredMethod("onActivityResult", int.class, int.class, Intent.class);
            method.setAccessible(true);
            method.invoke(activity, SCAN_BLUETOOTH, Activity.RESULT_OK, null);
        } catch (ReflectiveOperationException error) {
            throw new AssertionError(error);
        }
    }

    private static void invokeStartBluetooth(MainActivity activity) {
        try {
            Method method = MainActivity.class.getDeclaredMethod("startBluetooth");
            method.setAccessible(true);
            method.invoke(activity);
        } catch (ReflectiveOperationException error) {
            throw new AssertionError(error);
        }
    }

    private static void setLongField(Object target, String name, long value) throws Exception {
        Field field = target.getClass().getDeclaredField(name);
        field.setAccessible(true);
        field.setLong(target, value);
    }

    private static Object getField(Object target, String name) throws Exception {
        Field field = target.getClass().getDeclaredField(name);
        field.setAccessible(true);
        return field.get(target);
    }
}
