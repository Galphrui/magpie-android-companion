package com.ruiao.magpie;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Proxy;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLHandshakeException;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;
import org.json.JSONException;
import org.json.JSONObject;

final class BridgeClient {
    private static final String USB_BROWSER_ORIGIN = "https://127.0.0.1:43427";
    static final int MAX_LINE = 64 * 1024;
    private final String token;
    private final String fingerprint;
    private final byte[] fingerprintBytes;
    private final SSLContext tls;
    private final String base;
    private final String desktopOrigin;
    private final boolean bluetooth;

    BridgeClient(String code, String origin) throws GeneralSecurityException {
        if (!code.matches("[a-fA-F0-9]{64}:[A-Za-z0-9_-]{43}")) {
            throw new IllegalArgumentException("Invalid pairing format");
        }
        fingerprint = code.substring(0, 64).toLowerCase(java.util.Locale.ROOT);
        fingerprintBytes = new byte[32];
        for (int i = 0; i < fingerprintBytes.length; i++) {
            fingerprintBytes[i] = (byte) Integer.parseInt(fingerprint.substring(i * 2, i * 2 + 2), 16);
        }
        token = code.substring(65);
        bluetooth = BluetoothPairing.ORIGIN.equals(origin);
        desktopOrigin = bluetooth ? BluetoothPairing.BROWSER_ORIGIN
                : UsbPairingProvider.USB_ORIGIN.equals(origin) ? USB_BROWSER_ORIGIN : null;
        try {
            URL endpoint = new URL(origin);
            String host = endpoint.getHost();
            boolean privateIp = java.util.regex.Pattern.matches("10(?:\\.[0-9]{1,3}){3}|192\\.168(?:\\.[0-9]{1,3}){2}|172\\.(?:1[6-9]|2[0-9]|3[01])(?:\\.[0-9]{1,3}){2}", host);
            if (privateIp) {
                for (String octet : host.split("\\.")) if (Integer.parseInt(octet) > 255) privateIp = false;
            }
            if (!"https".equals(endpoint.getProtocol()) || endpoint.getUserInfo() != null || endpoint.getQuery() != null
                    || endpoint.getRef() != null || !("127.0.0.1".equals(host) || "localhost".equals(host) || privateIp)
                    || endpoint.getPort() < 1 || endpoint.getPort() > 65535 || endpoint.getPath().length() != 0) {
                throw new IllegalArgumentException("Invalid secure companion address");
            }
            base = "https://" + host + ":" + endpoint.getPort() + "/companion/v1";
        } catch (java.net.MalformedURLException e) { throw new IllegalArgumentException("Invalid secure companion address", e); }
        tls = SSLContext.getInstance("TLS");
        tls.init(null, new TrustManager[]{new X509TrustManager() {
            @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            @Override public void checkClientTrusted(X509Certificate[] chain, String auth) throws CertificateException {
                throw new CertificateException("Client certificates are not used");
            }
            @Override public void checkServerTrusted(X509Certificate[] chain, String auth) throws CertificateException {
                if (chain == null || chain.length == 0) throw new CertificateException("Missing server certificate");
                if (!matchesCertificate(chain[0])) throw new CertificateException("Pairing certificate does not match");
            }
        }}, null);
    }

    String desktopOrigin() { return desktopOrigin; }

    boolean isBluetooth() { return bluetooth; }

    boolean matchesPairing(String code) {
        return code.length() == 108 && fingerprint.equalsIgnoreCase(code.substring(0, 64))
                && token.equals(code.substring(65));
    }

    boolean matchesCertificate(X509Certificate certificate) {
        try {
            certificate.checkValidity();
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(certificate.getEncoded());
            return MessageDigest.isEqual(digest, fingerprintBytes);
        } catch (GeneralSecurityException e) {
            return false;
        }
    }

    String desktopCookie() throws IOException {
        if (desktopOrigin == null) throw new IOException("No compatible desktop origin");
        HttpsURLConnection connection = openUrl(desktopOrigin + "/companion-browser/session", "*/*");
        try {
            int status = connection.getResponseCode();
            String cookie = connection.getHeaderField("Set-Cookie");
            if ((status != 200 && status != 204 && status != 302 && status != 303)
                    || cookie == null || cookie.isEmpty()) {
                throw new IOException("Desktop session is unavailable");
            }
            return cookie;
        } finally {
            connection.disconnect();
        }
    }

    HttpsURLConnection openEvents() throws IOException { return open("events", "text/event-stream"); }

    private HttpsURLConnection open(String path, String accept) throws IOException {
        return openUrl(base + "/" + path, accept);
    }

    private HttpsURLConnection openUrl(String url, String accept) throws IOException {
        HttpsURLConnection connection = (HttpsURLConnection) new URL(url).openConnection(Proxy.NO_PROXY);
        connection.setSSLSocketFactory(tls.getSocketFactory());
        // Leave HTTPS's default hostname verification enabled; the cert has a loopback IP SAN.
        connection.setInstanceFollowRedirects(false);
        connection.setUseCaches(false);
        connection.setConnectTimeout(bluetooth ? 12000 : 3500);
        connection.setReadTimeout(bluetooth ? 90000 : 15000);
        connection.setRequestProperty("Authorization", "Bearer " + token);
        connection.setRequestProperty("Accept", accept);
        connection.setRequestProperty("Accept-Encoding", "identity");
        return connection;
    }

    int setCurrency(String currency, String revision) throws IOException, JSONException {
        if (!currency.matches("usd|cny|provider") || !revision.matches("[0-9a-f]{32}")) {
            throw new IllegalArgumentException("Invalid currency change");
        }
        byte[] body = new JSONObject().put("action", "setCurrency").put("value", currency)
                .put("expectedRevision", revision).toString().getBytes(StandardCharsets.UTF_8);
        HttpsURLConnection c = open("commands", "application/json");
        try {
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setFixedLengthStreamingMode(body.length);
            c.setRequestProperty("Content-Type", "application/json");
            try (OutputStream out = c.getOutputStream()) { out.write(body); }
            return c.getResponseCode();
        } finally { c.disconnect(); }
    }

    interface Listener { void snapshot(JSONObject snapshot); }

    static void readEvents(HttpsURLConnection c, Listener listener) throws IOException, JSONException {
        int status = c.getResponseCode();
        if (status == 401 || status == 403) throw new PairingException();
        String content = c.getContentType();
        if (status != 200 || content == null || !content.split(";", 2)[0].trim().equalsIgnoreCase("text/event-stream")) {
            throw new IOException("No event stream");
        }
        try (BufferedInputStream stream = new BufferedInputStream(c.getInputStream())) {
            String data = null;
            for (String line; (line = line(stream)) != null && !Thread.currentThread().isInterrupted(); ) {
                if (line.isEmpty() && data != null) {
                    JSONObject snapshot = new JSONObject(data);
                    if (snapshot.optInt("version", -1) != 1) throw new VersionException();
                    listener.snapshot(snapshot);
                    data = null;
                } else if (line.startsWith("data: ")) {
                    if (data != null) throw new IOException("Unexpected SSE format");
                    data = line.substring(6);
                }
            }
        }
    }

    static String line(InputStream stream) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream(256);
        for (int b; (b = stream.read()) != -1; ) {
            if (b == '\n') {
                byte[] raw = bytes.toByteArray();
                int size = raw.length;
                if (size > 0 && raw[size - 1] == '\r') size--;
                return new String(raw, 0, size, StandardCharsets.UTF_8);
            }
            if (bytes.size() >= MAX_LINE) throw new IOException("Event line too long");
            bytes.write(b);
        }
        return null;
    }

    static boolean badPairing(Throwable error) { return error instanceof SSLHandshakeException || error instanceof PairingException; }
    static final class PairingException extends IOException { }
    static final class VersionException extends IOException { }
}
