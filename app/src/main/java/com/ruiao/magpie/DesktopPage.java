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
import java.security.cert.CertificateFactory;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.Collection;
import java.util.List;

/** Shows the connected Mac's own Magpie browser UI over the authorized USB tunnel. */
final class DesktopPage {
    static final int PICK_FILES = 515;
    private static final String ORIGIN = BridgeClient.DESKTOP_ORIGIN;
    private final Activity activity;
    private final BridgeClient bridge;
    private final Runnable unavailable;
    final WebView view;
    private ValueCallback<Uri[]> filePicker;
    private boolean closed;

    DesktopPage(Activity activity, BridgeClient bridge, Runnable unavailable) {
        this.activity = activity;
        this.bridge = bridge;
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
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        CookieManager.getInstance().setAcceptThirdPartyCookies(view, false);
        view.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView current, WebResourceRequest request) {
                return !sameOrigin(request.getUrl());
            }

            @Override public WebResourceResponse shouldInterceptRequest(WebView current, WebResourceRequest request) {
                if (sameOrigin(request.getUrl())) return null;
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
                if (!closed && request.isForMainFrame()) unavailable.run();
            }

            @Override public void onReceivedHttpError(WebView current, WebResourceRequest request, WebResourceResponse response) {
                if (!closed && request.isForMainFrame() && response.getStatusCode() >= 400) unavailable.run();
            }

            @Override public boolean onRenderProcessGone(WebView current, android.webkit.RenderProcessGoneDetail detail) {
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
                    CookieManager.getInstance().setCookie(ORIGIN, sessionCookie, stored -> {
                        if (closed) return;
                        if (Boolean.TRUE.equals(stored)) view.loadUrl(ORIGIN + "/");
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
        view.stopLoading();
        if (view.getParent() instanceof android.view.ViewGroup) {
            ((android.view.ViewGroup) view.getParent()).removeView(view);
        }
        view.destroy();
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

    private static boolean sameOrigin(Uri uri) {
        return "https".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
                && uri.getPort() == 43427 && uri.getUserInfo() == null;
    }
}
