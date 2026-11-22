package com.ruiao.magpie;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.net.http.SslCertificate;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.security.cert.CertificateFactory;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.Collection;
import java.util.Collections;
import java.util.List;
import java.util.regex.Pattern;

/** Shows the connected computer's Magpie browser UI over its authenticated tunnel. */
final class DesktopPage {
    static final int PICK_FILES = 515;
    private static final Pattern STATIC_PATH = Pattern.compile("/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+(?i:\\.(?:m?js|css|json|webmanifest|svg|png|ico|webp|jpe?g|woff2?|ttf|avif))");
    private static final Pattern ASSET_VERSION = Pattern.compile("v=[0-9a-f]{12}");
    private final Activity activity;
    private final BridgeClient bridge;
    private final String origin;
    private final int port;
    private final Runnable unavailable;
    final WebView view;
    private ValueCallback<Uri[]> filePicker;
    private boolean closed, rendererGone;

    DesktopPage(Activity activity, BridgeClient bridge, Runnable loaded, Runnable unavailable) {
        this.activity = activity;
        this.bridge = bridge;
        this.origin = bridge.desktopOrigin();
        this.port = Uri.parse(origin).getPort();
        this.unavailable = unavailable;
        view = new WebView(activity);
        view.clearSslPreferences();
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setUseWideViewPort(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        // Immutable, hash-versioned browser assets are safe to cache on every transport.
        // Private APIs still obey the server's no-store headers.
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        CookieManager.getInstance().setAcceptThirdPartyCookies(view, false);
        // Keep the initial page load from stealing focus and opening the IME.
        // Web form controls still receive focus and show the keyboard normally
        // after an explicit user tap.
        view.setFocusableInTouchMode(true);
        view.requestFocus(android.view.View.FOCUS_DOWN);
        view.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView current, WebResourceRequest request) {
                return !sameOrigin(request.getUrl());
            }

            @Override public WebResourceResponse shouldInterceptRequest(WebView current, WebResourceRequest request) {
                if (sameOrigin(request.getUrl())) return cachedAsset(request);
                return new WebResourceResponse("text/plain", "UTF-8", new ByteArrayInputStream(new byte[0]));
            }

            @Override public void onReceivedSslError(WebView current, SslErrorHandler handler, SslError error) {
                String url = error.getUrl();
                if (!closed && url != null && sameOrigin(Uri.parse(url)) && checkCertificate(error.getCertificate())) {
                    handler.proceed();
                } else {
                    handler.cancel();
                }
            }

            @Override public void onReceivedError(WebView current, WebResourceRequest request, WebResourceError error) {
                if (!closed && requiredForPage(request)) unavailable.run();
            }

            @Override public void onReceivedHttpError(WebView current, WebResourceRequest request, WebResourceResponse response) {
                if (!closed && response.getStatusCode() >= 400 && requiredForPage(request)) unavailable.run();
            }

            @Override public void onPageFinished(WebView current, String url) {
                if (!closed && url != null && sameOrigin(Uri.parse(url))) loaded.run();
            }

            @Override public boolean onRenderProcessGone(WebView current, android.webkit.RenderProcessGoneDetail detail) {
                rendererGone = true;
                if (!closed) unavailable.run();
                return true;
            }
        });
        view.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView current, ValueCallback<Uri[]> result, FileChooserParams params) {
                if (closed || current.getUrl() == null || !sameOrigin(Uri.parse(current.getUrl()))) return false;
                finishFilePicker(null);
                filePicker = result;
                try {
                    activity.startActivityForResult(params.createIntent(), PICK_FILES);
                } catch (ActivityNotFoundException error) {
                    finishFilePicker(null);
                }
                return true;
            }
        });
    }

    void open() {
        new Thread(() -> {
            try {
                String sessionCookie = bridge.desktopCookie();
                activity.runOnUiThread(() -> {
                    if (closed) return;
                    CookieManager.getInstance().setCookie(origin, sessionCookie, stored -> {
                        if (closed) return;
                        if (Boolean.TRUE.equals(stored)) view.loadUrl(origin + "/?companion=android");
                        else unavailable.run();
                    });
                });
            } catch (IOException error) {
                activity.runOnUiThread(() -> { if (!closed) unavailable.run(); });
            }
        }, "magpie-desktop-session").start();
    }

    void filePickerResult(int result, Intent data) {
        Uri[] files = WebChromeClient.FileChooserParams.parseResult(result, data);
        if (files != null) {
            for (Uri file : files) {
                if (file == null || !"content".equals(file.getScheme())) {
                    files = null;
                    break;
                }
            }
        }
        finishFilePicker(files);
    }

    private void finishFilePicker(Uri[] files) {
        ValueCallback<Uri[]> pending = filePicker;
        filePicker = null;
        if (pending != null) pending.onReceiveValue(files);
    }

    void close() {
        closed = true;
        finishFilePicker(null);
        if (!rendererGone) view.stopLoading();
        if (view.getParent() instanceof android.view.ViewGroup) {
            ((android.view.ViewGroup) view.getParent()).removeView(view);
        }
        view.destroy();
    }

    private boolean requiredForPage(WebResourceRequest request) {
        Uri url = request.getUrl();
        if (!sameOrigin(url)) return false;
        String path = url.getPath();
        return request.isForMainFrame() || "/app.js".equals(path) || "/app.css".equals(path)
                || "/i18n.js".equals(path) || "/start.js".equals(path);
    }

    private WebResourceResponse cachedAsset(WebResourceRequest request) {
        if (request.isForMainFrame() || !"GET".equals(request.getMethod())) return null;
        Uri url = request.getUrl();
        String path = url.getEncodedPath();
        if ("/companion-android/magpie-interaction.js".equals(path)
                || "/companion-android/magpie-landscape.css".equals(path)) {
            try {
                boolean stylesheet = path.endsWith(".css");
                InputStream body = activity.getAssets().open(stylesheet
                        ? "magpie-landscape.css" : "magpie-interaction.js");
                return new WebResourceResponse(stylesheet ? "text/css" : "text/javascript", "UTF-8",
                        200, "OK", Collections.singletonMap("Cache-Control", "no-store"), body);
            } catch (IOException missing) {
                return null;
            }
        }
        String query = url.getEncodedQuery();
        if (path == null || query == null || !STATIC_PATH.matcher(path).matches()
                || !ASSET_VERSION.matcher(query).matches()) return null;
        try {
            // Only exact SHA-256-versioned immutable assets are read from the APK cache.
            InputStream body = activity.getAssets().open("magpie-web/" + query.substring(2) + path);
            String mime = mimeType(path);
            String encoding = mime.startsWith("text/") || "application/json".equals(mime)
                    || mime.endsWith("+json") ? "UTF-8" : null;
            return new WebResourceResponse(mime, encoding, 200, "OK",
                    Collections.singletonMap("Cache-Control", "private, max-age=31536000, immutable"), body);
        } catch (IOException missing) {
            return null;
        }
    }

    private static String mimeType(String path) {
        String lower = path.toLowerCase(java.util.Locale.ROOT);
        if (lower.endsWith(".css")) return "text/css";
        if (lower.endsWith(".js") || lower.endsWith(".mjs")) return "text/javascript";
        if (lower.endsWith(".json")) return "application/json";
        if (lower.endsWith(".webmanifest")) return "application/manifest+json";
        if (lower.endsWith(".svg")) return "image/svg+xml";
        if (lower.endsWith(".png")) return "image/png";
        if (lower.endsWith(".ico")) return "image/x-icon";
        if (lower.endsWith(".webp")) return "image/webp";
        if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
        if (lower.endsWith(".avif")) return "image/avif";
        if (lower.endsWith(".woff2")) return "font/woff2";
        if (lower.endsWith(".woff")) return "font/woff";
        if (lower.endsWith(".ttf")) return "font/ttf";
        return "application/octet-stream";
    }

    private boolean checkCertificate(SslCertificate sslCertificate) {
        if (sslCertificate == null) return false;
        try {
            X509Certificate cert;
            if (Build.VERSION.SDK_INT >= 29) {
                cert = sslCertificate.getX509Certificate();
            } else {
                Bundle state = SslCertificate.saveState(sslCertificate);
                byte[] bytes = state == null ? null : state.getByteArray("x509-certificate");
                if (bytes == null) return false;
                cert = (X509Certificate) CertificateFactory.getInstance("X.509")
                        .generateCertificate(new ByteArrayInputStream(bytes));
            }
            if (cert == null || !bridge.matchesCertificate(cert)) return false;
            Collection<List<?>> names = cert.getSubjectAlternativeNames();
            if (names == null) return false;
            for (List<?> name : names) {
                if (name.size() == 2 && Integer.valueOf(7).equals(name.get(0))
                        && "127.0.0.1".equals(name.get(1))) return true;
            }
        } catch (CertificateException error) {
            return false;
        }
        return false;
    }

    private boolean sameOrigin(Uri uri) {
        return "https".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
                && uri.getPort() == port && uri.getUserInfo() == null;
    }
}
