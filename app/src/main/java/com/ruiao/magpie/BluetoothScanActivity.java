package com.ruiao.magpie;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;
import com.google.zxing.BarcodeFormat;
import com.google.zxing.BinaryBitmap;
import com.google.zxing.RGBLuminanceSource;
import com.google.zxing.ReaderException;
import com.google.zxing.common.HybridBinarizer;
import com.google.zxing.qrcode.QRCodeReader;
import com.journeyapps.barcodescanner.DecoratedBarcodeView;
import com.journeyapps.barcodescanner.DefaultDecoderFactory;
import java.io.IOException;
import java.io.InputStream;
import java.util.Collections;

/** Local QR scanner. The result stays inside this process. */
public final class BluetoothScanActivity extends Activity {
    static final String VISIBLE_BARS = "com.ruiao.magpie.scan.VISIBLE_BARS";
    private static final int CAMERA_PERMISSION = 1;
    private static final int PICK_IMAGE = 2;
    private static final int MAX_IMAGE_SIDE = 1600;
    private static BluetoothPairing result;
    private DecoratedBarcodeView camera;
    private Button imagePicker;
    private boolean completed;
    private boolean canScanCamera;
    private boolean imagePickerOpen;

    static BluetoothPairing takeResult() {
        BluetoothPairing pairing = result;
        result = null;
        return pairing;
    }

    @Override protected void onCreate(Bundle savedState) {
        super.onCreate(savedState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE | WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        if (savedState == null) result = null;
        canScanCamera = getPackageManager().hasSystemFeature(PackageManager.FEATURE_CAMERA_ANY);
        imagePickerOpen = savedState != null && savedState.getBoolean("imagePickerOpen");
        imagePicker = new Button(this);
        imagePicker.setText("从图片识别");
        imagePicker.setEnabled(!imagePickerOpen);
        imagePicker.setOnClickListener(view -> pickImage());
        camera = new DecoratedBarcodeView(this);
        camera.setStatusText(canScanCamera ? "扫描电脑配置程序显示的蓝牙配对二维码" : "此设备没有可用相机");
        camera.getBarcodeView().setDecoderFactory(new DefaultDecoderFactory(Collections.singleton(BarcodeFormat.QR_CODE)));
        camera.decodeContinuous(barcode -> {
            if (imagePicker.isEnabled()) acceptQrText(barcode.getText());
        });
        FrameLayout layout = new FrameLayout(this);
        layout.addView(camera, new FrameLayout.LayoutParams(-1, -1));
        FrameLayout.LayoutParams buttonLayout = new FrameLayout.LayoutParams(-2, -2, Gravity.TOP | Gravity.END);
        int margin = Math.round(12 * getResources().getDisplayMetrics().density);
        buttonLayout.topMargin = margin;
        buttonLayout.setMarginEnd(margin);
        layout.addView(imagePicker, buttonLayout);
        ViewCompat.setOnApplyWindowInsetsListener(layout, (view, insets) -> {
            Insets safe = insets.getInsets(WindowInsetsCompat.Type.displayCutout() | visibleBars());
            view.setPadding(safe.left, safe.top, safe.right, safe.bottom);
            return WindowInsetsCompat.CONSUMED;
        });
        setContentView(layout);
        if (canScanCamera && savedState == null && checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.CAMERA}, CAMERA_PERMISSION);
        }
    }

    @Override protected void onSaveInstanceState(Bundle state) {
        super.onSaveInstanceState(state);
        state.putBoolean("imagePickerOpen", imagePickerOpen);
    }

    private void pickImage() {
        if (completed) return;
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType("image/*");
        imagePickerOpen = true;
        imagePicker.setEnabled(false);
        try {
            startActivityForResult(intent, PICK_IMAGE);
        } catch (ActivityNotFoundException error) {
            imagePickerOpen = false;
            imagePicker.setEnabled(true);
            camera.setStatusText("此设备无法选择图片");
        }
    }

    @Override protected void onActivityResult(int request, int code, Intent data) {
        super.onActivityResult(request, code, data);
        if (request != PICK_IMAGE || completed || isFinishing()) return;
        imagePickerOpen = false;
        Uri uri = code == RESULT_OK && data != null ? data.getData() : null;
        if (uri == null || !"content".equals(uri.getScheme())) {
            imagePicker.setEnabled(true);
            return;
        }
        camera.setStatusText("正在识别图片中的二维码…");
        new Thread(() -> {
            String text = readQrImage(uri);
            runOnUiThread(() -> {
                if (completed || isFinishing() || isDestroyed()) return;
                imagePicker.setEnabled(true);
                if (text == null) camera.setStatusText("未能从图片识别二维码，请选择清晰的配对码");
                else acceptQrText(text);
            });
        }, "Magpie QR image").start();
    }

    private String readQrImage(Uri uri) {
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inJustDecodeBounds = true;
        try {
            try (InputStream input = getContentResolver().openInputStream(uri)) {
                if (input == null) return null;
                BitmapFactory.decodeStream(input, null, options);
            }
            if (options.outWidth <= 0 || options.outHeight <= 0) return null;
            int largestSide = Math.max(options.outWidth, options.outHeight);
            options.inSampleSize = 1;
            while ((long) options.inSampleSize * MAX_IMAGE_SIDE < largestSide) options.inSampleSize *= 2;
            options.inJustDecodeBounds = false;
            try (InputStream input = getContentResolver().openInputStream(uri)) {
                if (input == null) return null;
                Bitmap bitmap = BitmapFactory.decodeStream(input, null, options);
                if (bitmap == null) return null;
                try {
                    int width = bitmap.getWidth(), height = bitmap.getHeight();
                    if (width > MAX_IMAGE_SIDE || height > MAX_IMAGE_SIDE) return null;
                    int[] pixels = new int[width * height];
                    bitmap.getPixels(pixels, 0, width, 0, 0, width, height);
                    RGBLuminanceSource source = new RGBLuminanceSource(width, height, pixels);
                    return new QRCodeReader().decode(new BinaryBitmap(new HybridBinarizer(source))).getText();
                } finally {
                    bitmap.recycle();
                }
            }
        } catch (IOException | ReaderException | IllegalArgumentException | SecurityException error) {
            return null;
        }
    }

    private void acceptQrText(String text) {
        if (completed || isFinishing() || isDestroyed()) return;
        try {
            complete(BluetoothPairing.parse(text));
        } catch (IllegalArgumentException error) {
            camera.setStatusText("二维码格式不正确，请扫描 Magpie 电脑端的配对码");
        }
    }

    @Override protected void onResume() {
        super.onResume();
        if (canScanCamera && checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            camera.resume();
        }
    }

    @Override protected void onPause() {
        if (camera != null) camera.pause();
        super.onPause();
    }

    @Override public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (!hasFocus) return;
        WindowInsetsControllerCompat controller = WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        controller.setSystemBarsBehavior(WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
        controller.show(visibleBars());
        controller.hide(WindowInsetsCompat.Type.systemBars() & ~visibleBars());
    }

    private int visibleBars() {
        return getIntent().getIntExtra(VISIBLE_BARS, 0) & WindowInsetsCompat.Type.systemBars();
    }

    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode != CAMERA_PERMISSION || camera == null || isFinishing()) return;
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) camera.resume();
        else camera.setStatusText("需要相机权限才能扫描配对二维码");
    }

    private void complete(BluetoothPairing pairing) {
        if (completed || isFinishing() || isDestroyed()) return;
        result = pairing;
        completed = true;
        setResult(RESULT_OK);
        finish();
    }
}
