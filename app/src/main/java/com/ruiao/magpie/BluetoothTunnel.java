package com.ruiao.magpie;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattServer;
import android.bluetooth.BluetoothGattServerCallback;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.BluetoothStatusCodes;
import android.bluetooth.le.AdvertiseCallback;
import android.bluetooth.le.AdvertiseData;
import android.bluetooth.le.AdvertiseSettings;
import android.bluetooth.le.BluetoothLeAdvertiser;
import android.content.Context;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.ParcelUuid;

import java.io.Closeable;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.BindException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.ArrayDeque;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.Semaphore;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.function.Consumer;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** Transfers only the companion's fixed TLS ports through an authenticated BLE GATT link. */
@SuppressLint("MissingPermission")
final class BluetoothTunnel implements Closeable {
    static final int SUMMARY_PORT = 43435;
    static final int BROWSER_PORT = 43437;
    private static final UUID WRITE_UUID = UUID.fromString("3a53a032-1eef-4c97-8c9a-15ac27a9f2a1");
    private static final UUID SEND_UUID = UUID.fromString("3a53a032-1eef-4c97-8c9a-15ac27a9f2a2");
    private static final UUID CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
    private static final int HELLO = 1, PHONE_NONCE = 2, PHONE_PROOF = 3, HOST_PROOF = 4, READY = 5;
    private static final int PING = 6, PONG = 7, LOG = 8;
    private static final int OPEN = 16, DATA = 17, FIN = 18, RESET = 19;
    private static final int HEADER_SIZE = 3, MIN_FRAME = 20, MAX_FRAME = 244;
    private static final int MAX_STREAMS = 12;
    private static final int NONCE_SIZE = 16;
    private static final long PAIR_TIMEOUT_MS = 15000, SEND_TIMEOUT_MS = 5000;
    private static final long HEARTBEAT_TIMEOUT_MS = 15000;
    private static final byte[] EMPTY = new byte[0];

    private final Context context;
    private final BluetoothManager manager;
    private final BluetoothLeAdvertiser advertiser;
    private final UUID serviceId;
    private final byte[] token;
    private final Consumer<String> status;
    private final Consumer<String> log;
    private final Consumer<Boolean> connected;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final HandlerThread thread = new HandlerThread("Magpie BLE");
    private final Handler handler;
    private final ExecutorService sockets = new ThreadPoolExecutor(2 + 2 * MAX_STREAMS, 2 + 2 * MAX_STREAMS,
            0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(2 * MAX_STREAMS));
    private final Semaphore pendingSockets = new Semaphore(MAX_STREAMS);
    private final SecureRandom random = new SecureRandom();
    private final Map<Integer, Stream> streams = new HashMap<>();
    private final ArrayDeque<Frame> outgoing = new ArrayDeque<>();
    private final Runnable indicationTimeout = () -> dropPeer("蓝牙响应超时，正在等待重连");
    private final Runnable heartbeatTimeout = () -> dropPeer("电脑蓝牙连接超时，请重新扫码");

    // GATT state is confined to handler. Socket workers only read frameSize and submit frames.
    private BluetoothGattServer gatt;
    private BluetoothGattCharacteristic sendCharacteristic;
    private BluetoothDevice peer;
    private ServerSocket summarySocket, browserSocket;
    private Frame sending;
    private byte[] hostNonce, phoneNonce;
    private final StringBuilder logBuffer = new StringBuilder();
    private Runnable pairingTimeout;
    private boolean serviceAdded, advertising, subscribed, indications;
    private int lastStreamId, bindAttempts;
    private volatile int frameSize = MIN_FRAME;
    private volatile boolean paired, closed;

    static BluetoothTunnel start(Context context, UUID serviceId, String token,
                                 Consumer<String> status, Consumer<String> log,
                                 Consumer<Boolean> connected) throws IOException {
        if (serviceId == null || token == null || !token.matches("[A-Za-z0-9_-]{43}")) {
            throw new IOException("蓝牙配对信息无效");
        }
        BluetoothManager manager = context.getSystemService(BluetoothManager.class);
        BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
        try {
            if (adapter == null) throw new IOException("手机没有可用的蓝牙适配器");
            if (!adapter.isEnabled()) throw new IOException("请先开启手机蓝牙");
            BluetoothLeAdvertiser advertiser = adapter.getBluetoothLeAdvertiser();
            if (advertiser == null) throw new IOException("此手机无法发布低功耗蓝牙服务");
            return new BluetoothTunnel(context, manager, advertiser, serviceId, token, status, log, connected);
        } catch (SecurityException error) {
            throw new IOException("请允许应用使用附近设备蓝牙权限", error);
        }
    }

    private BluetoothTunnel(Context context, BluetoothManager manager, BluetoothLeAdvertiser advertiser,
                            UUID serviceId, String token, Consumer<String> status, Consumer<String> log,
                            Consumer<Boolean> connected) {
        this.context = context.getApplicationContext();
        this.manager = manager;
        this.advertiser = advertiser;
        this.serviceId = serviceId;
        this.token = token.getBytes(StandardCharsets.US_ASCII);
        this.status = status;
        this.log = log;
        this.connected = connected;
        thread.start();
        handler = new Handler(thread.getLooper());
        postSafely(this::open);
    }

    boolean isPaired() {
        return paired && !closed;
    }

    boolean isClosed() {
        return closed;
    }

    private void open() {
        if (closed) return;
        try {
            summarySocket = listen(SUMMARY_PORT);
            browserSocket = listen(BROWSER_PORT);
            sockets.execute(() -> acceptSockets(summarySocket, 1));
            sockets.execute(() -> acceptSockets(browserSocket, 2));
            gatt = manager.openGattServer(context, gattCallback);
            if (gatt == null) {
                fail("无法开启手机蓝牙服务");
                return;
            }
            BluetoothGattService service = new BluetoothGattService(serviceId, BluetoothGattService.SERVICE_TYPE_PRIMARY);
            service.addCharacteristic(new BluetoothGattCharacteristic(WRITE_UUID,
                    BluetoothGattCharacteristic.PROPERTY_WRITE, BluetoothGattCharacteristic.PERMISSION_WRITE));
            sendCharacteristic = new BluetoothGattCharacteristic(SEND_UUID,
                    BluetoothGattCharacteristic.PROPERTY_INDICATE | BluetoothGattCharacteristic.PROPERTY_NOTIFY, 0);
            sendCharacteristic.addDescriptor(new BluetoothGattDescriptor(CCCD,
                    BluetoothGattDescriptor.PERMISSION_READ | BluetoothGattDescriptor.PERMISSION_WRITE));
            service.addCharacteristic(sendCharacteristic);
            if (!gatt.addService(service)) fail("无法注册手机蓝牙服务");
            else handler.postDelayed(() -> {
                if (!serviceAdded && !closed) fail("手机蓝牙服务启动超时");
            }, PAIR_TIMEOUT_MS);
        } catch (BindException error) {
            // Re-scanning can start a new tunnel while the previous one closes its listeners.
            closeQuietly(summarySocket);
            closeQuietly(browserSocket);
            summarySocket = browserSocket = null;
            if (++bindAttempts < 8) handler.postDelayed(this::open, 250);
            else fail("蓝牙本机通道端口已被占用");
        } catch (IOException error) {
            fail("无法启动蓝牙本机通道，请重试");
        } catch (SecurityException error) {
            fail("手机蓝牙权限不可用");
        }
    }

    private static ServerSocket listen(int port) throws IOException {
        ServerSocket socket = new ServerSocket();
        try {
            socket.setReuseAddress(true);
            socket.bind(new InetSocketAddress(InetAddress.getByAddress(new byte[]{127, 0, 0, 1}), port), MAX_STREAMS);
            return socket;
        } catch (IOException error) {
            closeQuietly(socket);
            throw error;
        }
    }

    private void advertise() {
        if (closed || peer != null || advertising) return;
        try {
            AdvertiseSettings settings = new AdvertiseSettings.Builder()
                    .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
                    .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
                    .setConnectable(true).build();
            AdvertiseData data = new AdvertiseData.Builder()
                    .addServiceUuid(new ParcelUuid(serviceId)).build();
            advertising = true;
            advertiser.startAdvertising(settings, data, advertiseCallback);
        } catch (IllegalStateException | SecurityException error) {
            fail("无法广播蓝牙配对，请检查蓝牙权限和开关");
        }
    }

    private final AdvertiseCallback advertiseCallback = new AdvertiseCallback() {
        @Override public void onStartSuccess(AdvertiseSettings settings) {
            postSafely(() -> {
                if (!closed && advertising) report("等待电脑通过蓝牙配对");
            });
        }

        @Override public void onStartFailure(int errorCode) {
            postSafely(() -> {
                if (!closed && advertising) fail("手机蓝牙广播启动失败（" + errorCode + "）");
            });
        }
    };

    private final BluetoothGattServerCallback gattCallback = new BluetoothGattServerCallback() {
        @Override public void onServiceAdded(int result, BluetoothGattService service) {
            postSafely(() -> {
                if (closed) return;
                if (result == BluetoothGatt.GATT_SUCCESS && serviceId.equals(service.getUuid())) {
                    serviceAdded = true;
                    advertise();
                } else {
                    fail("无法注册手机蓝牙服务（" + result + "）");
                }
            });
        }

        @Override public void onConnectionStateChange(BluetoothDevice device, int result, int state) {
            postSafely(() -> {
                if (closed || gatt == null) return;
                if (result == BluetoothGatt.GATT_SUCCESS && state == BluetoothProfile.STATE_CONNECTED) {
                    if (peer != null && !peer.equals(device)) {
                        cancel(device);
                        return;
                    }
                    if (peer != null) return;
                    peer = device;
                    stopAdvertising();
                    report("蓝牙已连接，正在验证二维码");
                    pairingTimeout = () -> {
                        if (!paired && device.equals(peer)) dropPeer("蓝牙配对超时，正在等待重连");
                    };
                    handler.postDelayed(pairingTimeout, PAIR_TIMEOUT_MS);
                } else if (device.equals(peer)) {
                    clearPeer();
                    advertise();
                }
            });
        }

        @Override public void onMtuChanged(BluetoothDevice device, int mtu) {
            postSafely(() -> {
                if (!closed && device.equals(peer)) frameSize = Math.max(MIN_FRAME, Math.min(MAX_FRAME, mtu - 3));
            });
        }

        @Override public void onDescriptorReadRequest(BluetoothDevice device, int requestId, int offset,
                                                       BluetoothGattDescriptor descriptor) {
            postSafely(() -> {
                boolean valid = device.equals(peer) && canReadSubscription(descriptor) && offset == 0;
                byte[] value = !subscribed ? BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE
                        : indications ? BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
                        : BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE;
                respond(device, requestId, valid, valid ? value : null);
            });
        }

        @Override public void onDescriptorWriteRequest(BluetoothDevice device, int requestId,
                                                        BluetoothGattDescriptor descriptor,
                                                        boolean preparedWrite, boolean responseNeeded,
                                                        int offset, byte[] value) {
            postSafely(() -> {
                if (closed) return;
                boolean indication = Arrays.equals(BluetoothGattDescriptor.ENABLE_INDICATION_VALUE, value);
                boolean notification = Arrays.equals(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE, value);
                boolean enabled = indication || notification;
                boolean disabled = Arrays.equals(BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE, value);
                boolean valid = peer != null && device.equals(peer) && canReadSubscription(descriptor)
                        && offset == 0 && !preparedWrite && (enabled || disabled);
                if (valid) {
                    subscribed = enabled;
                    indications = indication;
                }
                if (responseNeeded) respond(device, requestId, valid, null);
                if (valid && disabled && hostNonce != null) dropPeer("蓝牙已取消订阅，正在等待重连");
            });
        }

        @Override public void onCharacteristicWriteRequest(BluetoothDevice device, int requestId,
                                                             BluetoothGattCharacteristic characteristic,
                                                             boolean preparedWrite, boolean responseNeeded,
                                                             int offset, byte[] value) {
            byte[] frame = value == null ? EMPTY : value.clone();
            postSafely(() -> {
                if (closed) return;
                // Bulk DATA arrives as write-without-response; the pre-auth
                // handshake must still use a confirmed write for reliability.
                boolean valid = device.equals(peer) && WRITE_UUID.equals(characteristic.getUuid())
                        && offset == 0 && !preparedWrite && (paired || responseNeeded) && process(frame);
                if (responseNeeded) respond(device, requestId, valid, null);
                if (!valid && device.equals(peer)) {
                    dropPeer("蓝牙验证或通信失败，请确认正在使用桌面端的二维码");
                }
            });
        }

        @Override public void onNotificationSent(BluetoothDevice device, int result) {
            postSafely(() -> {
                if (closed || !device.equals(peer) || sending == null) return;
                if (result != BluetoothGatt.GATT_SUCCESS) {
                    dropPeer("蓝牙数据发送失败，正在等待重连");
                    return;
                }
                Frame frame = sending;
                sending = null;
                handler.removeCallbacks(indicationTimeout);
                if (frame.completion != null) frame.completion.complete(null);
                if (frame.bytes[0] == READY) {
                    paired = true;
                    if (pairingTimeout != null) handler.removeCallbacks(pairingTimeout);
                    handler.postDelayed(heartbeatTimeout, HEARTBEAT_TIMEOUT_MS);
                    report("蓝牙已配对");
                    reportConnection(true);
                }
                sendNext();
            });
        }
    };

    private boolean canReadSubscription(BluetoothGattDescriptor descriptor) {
        return CCCD.equals(descriptor.getUuid()) && SEND_UUID.equals(descriptor.getCharacteristic().getUuid());
    }

    private void respond(BluetoothDevice device, int request, boolean valid, byte[] value) {
        if (closed || gatt == null) return;
        try {
            gatt.sendResponse(device, request, valid ? BluetoothGatt.GATT_SUCCESS : BluetoothGatt.GATT_FAILURE, 0, value);
        } catch (SecurityException error) {
            fail("手机蓝牙权限不可用");
        }
    }

    private boolean process(byte[] frame) {
        if (!subscribed || frame.length < HEADER_SIZE || frame.length > frameSize) return false;
        int op = frame[0] & 255;
        int id = ((frame[1] & 255) << 8) | (frame[2] & 255);
        int length = frame.length - HEADER_SIZE;
        if (id == 0) {
            if (!paired) return authenticate(op, frame);
            if (op == LOG) {
                acceptLog(frame);
                return true;
            }
            if (op != PING || length != 0) return false;
            handler.removeCallbacks(heartbeatTimeout);
            handler.postDelayed(heartbeatTimeout, HEARTBEAT_TIMEOUT_MS);
            queue(frame(PONG, 0, EMPTY));
            return true;
        }
        if (!paired || (op != DATA && op != FIN && op != RESET)) return false;
        if ((op == DATA && length == 0) || (op != DATA && length != 0)) return false;
        Stream stream = streams.get(id);
        if (stream == null) return true; // A frame already in flight can arrive after a reset.
        if (op == RESET) {
            closeStream(stream);
        } else if (stream.remoteFinished || !stream.incoming.offer(
                op == DATA ? Arrays.copyOfRange(frame, HEADER_SIZE, frame.length) : EMPTY)) {
            closeStream(stream);
            queue(frame(RESET, id, EMPTY));
        } else if (op == FIN) {
            stream.remoteFinished = true;
        }
        return true;
    }

    private void acceptLog(byte[] frame) {
        String piece = new String(frame, HEADER_SIZE, frame.length - HEADER_SIZE, StandardCharsets.UTF_8);
        if (piece.isEmpty()) {
            if (logBuffer.length() > 0) {
                final String line = logBuffer.toString();
                logBuffer.setLength(0);
                if (log != null) ui.post(() -> log.accept(line));
            }
            return;
        }
        logBuffer.append(piece);
        if (logBuffer.length() > 4096) logBuffer.setLength(0);
    }

    private boolean authenticate(int op, byte[] frame) {
        if (frame.length != HEADER_SIZE + NONCE_SIZE) return false;
        byte[] payload = Arrays.copyOfRange(frame, HEADER_SIZE, frame.length);
        if (op == HELLO && hostNonce == null) {
            hostNonce = payload;
            phoneNonce = new byte[NONCE_SIZE];
            random.nextBytes(phoneNonce);
            queue(frame(PHONE_NONCE, 0, phoneNonce));
            queue(frame(PHONE_PROOF, 0, proof(token, hostNonce, phoneNonce, "phone")));
            return true;
        }
        if (op == HOST_PROOF && hostNonce != null && phoneNonce != null
                && MessageDigest.isEqual(proof(token, hostNonce, phoneNonce, "host"), payload)) {
            phoneNonce = null; // The same proof cannot yield a second READY.
            queue(frame(READY, 0, EMPTY));
            return true;
        }
        return false;
    }

    static byte[] proof(byte[] token, byte[] hostNonce, byte[] phoneNonce, String role) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(token, "HmacSHA256"));
            mac.update(("magpie-ble-v1/" + role).getBytes(StandardCharsets.US_ASCII));
            mac.update(hostNonce);
            return Arrays.copyOf(mac.doFinal(phoneNonce), NONCE_SIZE);
        } catch (GeneralSecurityException error) {
            throw new IllegalStateException(error);
        }
    }

    private static Frame frame(int op, int id, byte[] data) {
        return new Frame(encode(op, id, data));
    }

    static byte[] encode(int op, int id, byte[] data) {
        byte[] bytes = new byte[HEADER_SIZE + data.length];
        bytes[0] = (byte) op;
        bytes[1] = (byte) (id >>> 8);
        bytes[2] = (byte) id;
        System.arraycopy(data, 0, bytes, HEADER_SIZE, data.length);
        return bytes;
    }

    private static final class Frame {
        final byte[] bytes;
        CompletableFuture<Void> completion;

        Frame(byte[] bytes) {
            this.bytes = bytes;
        }

        void failed() {
            if (completion != null) completion.completeExceptionally(new IOException("Bluetooth link closed"));
        }
    }

    private void queue(Frame frame) {
        if (closed || peer == null || outgoing.size() >= MAX_STREAMS * 2) {
            frame.failed();
            if (!closed && peer != null) dropPeer("蓝牙发送队列已满，正在等待重连");
            return;
        }
        outgoing.add(frame);
        // Finish responding to the current ATT request before sending an indication.
        postSafely(this::sendNext);
    }

    private void sendNext() {
        if (closed || !subscribed || peer == null || sending != null || outgoing.isEmpty()) return;
        Frame frame = outgoing.remove();
        sending = frame;
        try {
            boolean sent;
            if (Build.VERSION.SDK_INT >= 33) {
                sent = gatt.notifyCharacteristicChanged(peer, sendCharacteristic, indications, frame.bytes)
                        == BluetoothStatusCodes.SUCCESS;
            } else {
                sendCharacteristic.setValue(frame.bytes);
                // confirm=false uses a notification: no per-frame ATT handshake, so
                // the controller can pipeline frames and reach far higher throughput.
                sent = gatt.notifyCharacteristicChanged(peer, sendCharacteristic, indications);
            }
            if (!sent) {
                dropPeer("蓝牙数据发送失败，正在等待重连");
                return;
            }
            handler.postDelayed(indicationTimeout, SEND_TIMEOUT_MS);
        } catch (SecurityException error) {
            fail("手机蓝牙权限不可用");
        }
    }

    private void acceptSockets(ServerSocket listener, int channel) {
        while (!closed) {
            try {
                Socket socket = listener.accept();
                if (!pendingSockets.tryAcquire()) {
                    closeQuietly(socket);
                    continue;
                }
                if (!postSafely(() -> {
                    try {
                        openStream(socket, channel);
                    } finally {
                        pendingSockets.release();
                    }
                })) {
                    pendingSockets.release();
                    closeQuietly(socket);
                }
            } catch (IOException error) {
                postSafely(() -> {
                    if (!closed) fail("蓝牙本机通道已关闭");
                });
                return;
            }
        }
    }

    private void openStream(Socket socket, int channel) {
        if (!isPaired() || streams.size() >= MAX_STREAMS) {
            closeQuietly(socket);
            return;
        }
        try {
            socket.setTcpNoDelay(true);
        } catch (IOException error) {
            closeQuietly(socket);
            return;
        }
        do {
            lastStreamId = lastStreamId % 65535 + 1;
        } while (streams.containsKey(lastStreamId));
        Stream stream = new Stream(lastStreamId, socket);
        streams.put(stream.id, stream);
        queue(frame(OPEN, stream.id, new byte[]{(byte) channel}));
        try {
            sockets.execute(stream::read);
            sockets.execute(stream::write);
        } catch (RejectedExecutionException error) {
            resetStream(stream);
        }
    }

    private final class Stream {
        final int id;
        final Socket socket;
        final ArrayBlockingQueue<byte[]> incoming = new ArrayBlockingQueue<>(256);
        boolean localFinished, remoteFinished, writerFinished;

        Stream(int id, Socket socket) {
            this.id = id;
            this.socket = socket;
        }

        void read() {
            try {
                InputStream in = socket.getInputStream();
                byte[] buffer = new byte[MAX_FRAME - HEADER_SIZE];
                int length;
                while ((length = in.read(buffer, 0, frameSize - HEADER_SIZE)) >= 0) {
                    if (length > 0) send(DATA, Arrays.copyOf(buffer, length));
                }
                send(FIN, EMPTY);
                postSafely(() -> {
                    localFinished = true;
                    if (writerFinished) closeStream(this);
                });
            } catch (IOException error) {
                postSafely(() -> resetStream(this));
            }
        }

        void send(int op, byte[] value) throws IOException {
            Frame next = frame(op, id, value);
            next.completion = new CompletableFuture<>();
            if (!postSafely(() -> {
                if (streams.get(id) == this) queue(next);
                else next.failed();
            })) throw new IOException("Bluetooth link closed");
            try {
                next.completion.get(15, TimeUnit.SECONDS);
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new IOException(error);
            } catch (ExecutionException | TimeoutException error) {
                throw new IOException(error);
            }
        }

        void write() {
            try {
                OutputStream output = socket.getOutputStream();
                while (true) {
                    byte[] value = incoming.take();
                    if (value == EMPTY) break;
                    output.write(value);
                }
                socket.shutdownOutput();
                postSafely(() -> {
                    writerFinished = true;
                    if (localFinished) closeStream(this);
                });
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
            } catch (IOException error) {
                postSafely(() -> resetStream(this));
            }
        }

        void close() {
            closeQuietly(socket);
            incoming.clear();
            incoming.offer(EMPTY);
        }
    }

    private void closeStream(Stream stream) {
        if (streams.remove(stream.id, stream)) stream.close();
    }

    private void resetStream(Stream stream) {
        if (streams.get(stream.id) != stream) return;
        closeStream(stream);
        queue(frame(RESET, stream.id, EMPTY));
    }

    private void clearPeer() {
        boolean hadPeerState = peer != null || paired || hostNonce != null || phoneNonce != null
                || subscribed || sending != null || !streams.isEmpty() || !outgoing.isEmpty();
        if (pairingTimeout != null) handler.removeCallbacks(pairingTimeout);
        handler.removeCallbacks(indicationTimeout);
        handler.removeCallbacks(heartbeatTimeout);
        for (Stream stream : streams.values()) stream.close();
        streams.clear();
        if (sending != null) sending.failed();
        sending = null;
        for (Frame frame : outgoing) frame.failed();
        outgoing.clear();
        peer = null;
        hostNonce = phoneNonce = null;
        subscribed = false;
        indications = false;
        frameSize = MIN_FRAME;
        paired = false;
        if (hadPeerState) reportConnection(false);
    }

    private void dropPeer(String message) {
        BluetoothDevice device = peer;
        clearPeer();
        if (device != null) cancel(device);
        if (closed) return;
        report(message);
        handler.postDelayed(this::advertise, 600);
    }

    private void cancel(BluetoothDevice device) {
        try {
            if (gatt != null) gatt.cancelConnection(device);
        } catch (SecurityException ignored) { }
    }

    private void stopAdvertising() {
        if (!advertising) return;
        advertising = false;
        try {
            advertiser.stopAdvertising(advertiseCallback);
        } catch (SecurityException ignored) { }
    }

    private void report(String message) {
        ui.post(() -> status.accept(message));
    }

    private void reportConnection(boolean active) {
        ui.post(() -> connected.accept(active && isPaired()));
    }

    private synchronized boolean postSafely(Runnable action) {
        return !closed && handler.post(action);
    }

    private void fail(String message) {
        report(message);
        close();
    }

    @Override public synchronized void close() {
        if (closed) return;
        closed = true;
        handler.post(() -> {
            BluetoothDevice device = peer;
            clearPeer();
            stopAdvertising();
            if (device != null) cancel(device);
            if (gatt != null) {
                try {
                    gatt.close();
                } catch (SecurityException ignored) { }
                gatt = null;
            }
            closeQuietly(summarySocket);
            closeQuietly(browserSocket);
            sockets.shutdownNow();
            Arrays.fill(token, (byte) 0);
            thread.quitSafely();
        });
    }

    private static void closeQuietly(Closeable closeable) {
        if (closeable == null) return;
        try {
            closeable.close();
        } catch (IOException ignored) { }
    }
}
