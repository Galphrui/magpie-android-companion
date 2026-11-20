package com.ruiao.magpie;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.content.res.Configuration;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.text.DateFormat;
import java.util.Date;
import javax.net.ssl.HttpsURLConnection;
import org.json.JSONArray;
import org.json.JSONObject;

public final class MainActivity extends Activity {
    private static final String[] PAGES = {"智能体", "网关", "路由", "设置"};
    private static final String[] GLYPHS = {"◈", "◎", "⋔", "⚙"};
    private static final String[] CURRENCIES = {"usd", "cny", "provider"};
    private static final String[] CURRENCY_NAMES = {"美元（USD）", "人民币（CNY）", "随供应商"};
    private static final String[] ORIENTATIONS = {"横屏（默认）", "竖屏", "跟随系统"};
    private static BridgeClient paired;
    private static String pairedOrigin = "https://127.0.0.1:43425";
    private static boolean commandPending;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private Stream stream;
    private boolean active, live, resumed;
    private String status = "USB 未连接";
    private JSONObject snapshot;
    private String displayKey = "";
    private long sampledAt;
    private int page, scrollY;
    private LinearLayout root, nav, content;
    private ScrollView scroll;
    private TextView connection;
    private boolean wide;
    private int bg, surface, text, muted, border, selected, green;
    private AlertDialog activeDialog;
    private DesktopPage desktop;
    private BridgeClient attemptedDesktop;
    private boolean summarySelected, desktopFailed;

    @Override protected void onCreate(Bundle state) {
        boolean night = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
        setTheme(night ? android.R.style.Theme_Material_NoActionBar : android.R.style.Theme_Material_Light_NoActionBar);
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        if (state != null) { page = Math.max(0, Math.min(PAGES.length - 1, state.getInt("page"))); scrollY = state.getInt("scroll"); summarySelected = state.getBoolean("summary"); }
        loadPairing(); applyOrientation(); makeLayout(); paint(false);
    }
    private void loadPairing() {
        PairingStore.Pairing saved = PairingStore.load(this);
        if (saved == null) { paired = null; pairedOrigin = UsbPairingProvider.USB_ORIGIN; return; }
        if (paired != null && pairedOrigin.equals(saved.origin) && paired.matchesPairing(saved.code)) return;
        try { paired = new BridgeClient(saved.code, saved.origin); pairedOrigin = saved.origin; }
        catch (Exception ignored) { PairingStore.clear(this); paired = null; pairedOrigin = UsbPairingProvider.USB_ORIGIN; }
    }
    @Override protected void onSaveInstanceState(Bundle state) {
        super.onSaveInstanceState(state); state.putInt("page", page); state.putInt("scroll", scroll == null ? scrollY : scroll.getScrollY()); state.putBoolean("summary", summarySelected);
    }
    @Override protected void onStart() { super.onStart(); active = true; loadPairing(); connect(); }
    @Override protected void onResume() { super.onResume(); resumed = true; if (desktop != null) desktop.view.onResume(); }
    @Override protected void onPause() { resumed = false; if (desktop != null) desktop.view.onPause(); if (activeDialog != null) activeDialog.dismiss(); super.onPause(); }
    @Override protected void onStop() { active = false; stopStream(); live = false; super.onStop(); }
    @Override protected void onDestroy() { closeDesktop(); super.onDestroy(); }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == DesktopPage.PICK_FILES && desktop != null) desktop.filePickerResult(result, data);
    }
    @Override public void onBackPressed() {
        if (desktop == null) { super.onBackPressed(); return; }
        if (desktop.view.canGoBack()) desktop.view.goBack();
        else showSummary(false);
    }
    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent); setIntent(intent);
        if ((getPackageName() + UsbPairingProvider.ACTION_USB_PAIR_UPDATED).equals(intent.getAction())) {
            stopStream(); loadPairing();
            if (desktopFailed) attemptedDesktop = null;
            snapshot = null; sampledAt = 0; displayKey = ""; connect();
        }
    }
    @Override public void onConfigurationChanged(Configuration config) {
        super.onConfigurationChanged(config); if (!active || desktop != null) return;
        if (scroll != null) scrollY = scroll.getScrollY(); displayKey = ""; makeLayout(); paint(false);
    }
    private int orientation() { return getPreferences(MODE_PRIVATE).getInt("orientation", 0); }
    private void applyOrientation() {
        int value = orientation(); int requested = value == 0 ? ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                : value == 1 ? ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT : ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED;
        if (getRequestedOrientation() != requested) setRequestedOrientation(requested);
    }
    private void connect() {
        stopStream(); live = false;
        if (desktop != null && attemptedDesktop != paired) {
            closeDesktop(); makeLayout(); displayKey = "";
        }
        if (active && hasDesktopPairing() && !summarySelected && attemptedDesktop != paired) openDesktop();
        if (desktop != null) return;
        status = paired == null ? "USB 未连接" : "正在连接电脑"; paint(true);
        if (active && paired != null) { stream = new Stream(paired); stream.start(); }
    }
    private boolean hasDesktopPairing() { return paired != null && UsbPairingProvider.USB_ORIGIN.equals(pairedOrigin); }
    private void openDesktop() {
        if (desktop != null || !hasDesktopPairing()) return;
        attemptedDesktop = paired; desktopFailed = false; summarySelected = false;
        stopStream();
        desktop = new DesktopPage(this, paired, () -> showSummary(true));
        LinearLayout frame = column(); frame.setBackgroundColor(bg); fitSystemBars(frame);
        LinearLayout top = row(); padding(top, 12, 0);
        top.addView(label("Magpie · USB", 14, text, true), new LinearLayout.LayoutParams(0, -2, 1));
        top.addView(button("手机设置", false, () -> showSummary(false)), lp(-2, -2));
        frame.addView(top, lp(-1, -2)); divider(frame);
        frame.addView(desktop.view, new LinearLayout.LayoutParams(-1, 0, 1));
        setContentView(frame);
        desktop.open();
    }
    private void closeDesktop() {
        if (desktop == null) return;
        DesktopPage previous = desktop; desktop = null;
        previous.close();
    }
    private void showSummary(boolean failed) {
        closeDesktop(); desktopFailed = failed; summarySelected = !failed;
        if (!failed) page = 3;
        displayKey = ""; scrollY = 0; makeLayout(); connect();
    }
    private void stopStream() { Stream old = stream; stream = null; if (old != null) old.close(); }
    private final class Stream extends Thread {
        final BridgeClient client; volatile boolean closed, received; volatile HttpsURLConnection link;
        Stream(BridgeClient client) { super("magpie-sse"); this.client = client; setDaemon(true); }
        void close() { closed = true; interrupt(); HttpsURLConnection c = link; if (c != null) new Thread(c::disconnect, "magpie-close").start(); }
        @Override public void run() {
            long delay = 1000;
            while (!closed) {
                received = false;
                try {
                    link = client.openEvents(); if (closed) return;
                    BridgeClient.readEvents(link, value -> { received = true; ui.post(() -> {
                        if (stream != this || !active) return;
                        displayKey = ""; snapshot = value; sampledAt = System.currentTimeMillis();
                        boolean ready = value.optBoolean("upstreamAvailable", false);
                        updateLink(true, transportName() + " 已连接" + (ready ? "" : " · 部分数据不可用"));
                    }); });
                } catch (Exception error) {
                    if (closed) return;
                    if (BridgeClient.badPairing(error) || error instanceof BridgeClient.VersionException) {
                        final String why = error instanceof BridgeClient.VersionException ? "版本不兼容，请检查电脑端同步桥" : "电脑配对已失效，请重新通过 USB 连接";
                        ui.post(() -> { if (stream == this) updateLink(false, why); }); return;
                    }
                } finally { HttpsURLConnection c = link; link = null; if (c != null) c.disconnect(); }
                ui.post(() -> { if (stream == this) updateLink(false, transportName() + " 暂不可达，正在重试"); });
                if (received) delay = 1000;
                try { Thread.sleep(delay); } catch (InterruptedException stop) { return; }
                delay = Math.min(delay * 2, 15000);
            }
        }
    }
    private String transportName() { return pairedOrigin.contains("127.0.0.1") || pairedOrigin.contains("localhost") ? "USB" : "局域网"; }
    private void updateLink(boolean connected, String label) { if (live != connected || !status.equals(label)) displayKey = ""; live = connected; status = label; paint(true); }
    private int dp(float n) { return Math.round(n * getResources().getDisplayMetrics().density); }
    private static int rgb(String hex) { return Color.parseColor(hex); }
    private static LinearLayout.LayoutParams lp(int w, int h) { return new LinearLayout.LayoutParams(w, h); }
    private LinearLayout column() { LinearLayout x = new LinearLayout(this); x.setOrientation(LinearLayout.VERTICAL); return x; }
    private LinearLayout row() { LinearLayout x = new LinearLayout(this); x.setOrientation(LinearLayout.HORIZONTAL); x.setGravity(Gravity.CENTER_VERTICAL); return x; }
    private void padding(View v, int a, int b) { v.setPadding(dp(a), dp(b), dp(a), dp(b)); }
    private TextView label(String title, float size, int color, boolean strong) { TextView v = new TextView(this); v.setText(title); v.setTextColor(color); v.setTextSize(size); v.setTypeface(Typeface.create(strong ? "sans-serif-medium" : "sans-serif", Typeface.NORMAL)); return v; }
    private GradientDrawable shape(int fill, int stroke, int radius) { GradientDrawable d = new GradientDrawable(); d.setColor(fill); d.setCornerRadius(dp(radius)); if (stroke != Color.TRANSPARENT) d.setStroke(dp(1), stroke); return d; }
    private TextView button(String name, boolean on, Runnable action) {
        TextView b = label(name, 14, text, on); b.setGravity(Gravity.CENTER); b.setMinHeight(dp(48)); padding(b, 8, 3);
        b.setBackground(new RippleDrawable(ColorStateList.valueOf(muted), shape(on ? selected : surface, on ? border : Color.TRANSPARENT, 9), shape(Color.WHITE, Color.TRANSPARENT, 9)));
        b.setFocusable(true); b.setOnClickListener(v -> action.run()); return b;
    }
    private void fitSystemBars(View view) {
        if (android.os.Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            view.setOnApplyWindowInsetsListener((v, insets) -> {
                android.graphics.Insets safe = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                v.setPadding(safe.left, safe.top, safe.right, safe.bottom);
                return insets;
            });
        }
    }
    private void makeLayout() {
        boolean dark = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
        bg = rgb(dark ? "#1A1A1E" : "#F4F4F6"); surface = rgb(dark ? "#222227" : "#FFFFFF"); text = rgb(dark ? "#F1F1F4" : "#24242A");
        muted = rgb(dark ? "#AEAEB9" : "#5E5E6B"); border = rgb(dark ? "#3A3A43" : "#DEDEE5"); selected = rgb(dark ? "#37373E" : "#E7E7ED"); green = rgb(dark ? "#8BD7AC" : "#226342");
        wide = getResources().getConfiguration().screenWidthDp >= 680; root = column(); root.setBackgroundColor(bg);
        getWindow().setStatusBarColor(bg); getWindow().setNavigationBarColor(bg); getWindow().getDecorView().setSystemUiVisibility(dark ? 0 : View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        fitSystemBars(root);
        LinearLayout top = row(); padding(top, 14, 4); top.addView(label("magpie", 20, text, true), new LinearLayout.LayoutParams(0, -2, 1));
        connection = button(live ? transportName() + " 已连接" : "USB 未连接", false, this::pairDialog); connection.setMaxLines(2); top.addView(connection, lp(-2, -2)); root.addView(top, lp(-1, -2)); divider(root);
        LinearLayout middle = wide ? row() : column(); if (wide) middle.setGravity(Gravity.TOP); root.addView(middle, new LinearLayout.LayoutParams(-1, 0, 1));
        nav = wide ? column() : row(); padding(nav, 7, 4); nav.setBackgroundColor(surface);
        if (wide) { middle.addView(nav, lp(dp(166), -1)); View line = new View(this); line.setBackgroundColor(border); middle.addView(line, lp(dp(1), -1)); }
        scroll = new ScrollView(this); scroll.setFillViewport(true); scroll.setClipToPadding(false); content = column(); padding(content, wide ? 24 : 15, 14); scroll.addView(content, new ScrollView.LayoutParams(-1, -2));
        middle.addView(scroll, wide ? new LinearLayout.LayoutParams(0, -1, 1) : lp(-1, -1)); if (!wide) { divider(root); root.addView(nav, lp(-1, -2)); }
        setContentView(root); drawNav();
    }
    private void drawNav() {
        nav.removeAllViews(); for (int i = 0; i < PAGES.length; i++) { final int dest = i;
            TextView item = button(GLYPHS[i] + (wide ? "   " : "\n") + PAGES[i], page == i, () -> { if (page == dest) return; scrollY = 0; page = dest; displayKey = ""; drawNav(); paint(false); });
            item.setContentDescription(PAGES[i]); item.setSelected(i == page); if (wide) { item.setGravity(Gravity.START | Gravity.CENTER_VERTICAL); padding(item, 13, 3); }
            nav.addView(item, wide ? lp(-1, -2) : new LinearLayout.LayoutParams(0, -2, 1));
        }
    }
    private void divider(LinearLayout view) { View v = new View(this); v.setBackgroundColor(border); view.addView(v, lp(-1, dp(1))); }
    private void gap(LinearLayout view, int space) { view.addView(new View(this), lp(1, dp(space))); }
    private void note(String value) { TextView v = label(value, 13, muted, false); v.setLineSpacing(dp(3), 1); content.addView(v); gap(content, 12); }
    private LinearLayout panel() { LinearLayout view = column(); padding(view, 14, 4); view.setBackground(shape(surface, border, 12)); content.addView(view, lp(-1, -2)); gap(content, 12); return view; }
    private void entry(LinearLayout parent, String title, String value, int color) {
        LinearLayout r = row(); r.setMinimumHeight(dp(48)); padding(r, 0, 7); TextView k = label(title, 14, text, true);
        TextView v = label(value, 14, color, false); v.setGravity(Gravity.END | Gravity.CENTER_VERTICAL); v.setTextIsSelectable(true); r.addView(k, new LinearLayout.LayoutParams(0, -2, 1));
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, -2, 1); p.setMarginStart(dp(12)); r.addView(v, p); if (parent.getChildCount() > 0) divider(parent); parent.addView(r, lp(-1, -2));
    }
    private JSONObject section(String name) { JSONObject child = snapshot == null ? null : snapshot.optJSONObject(name); return child == null ? new JSONObject() : child; }
    private JSONArray items(String name) { return snapshot == null ? null : snapshot.optJSONArray(name); }
    private boolean upstream() { return snapshot != null && snapshot.optBoolean("upstreamAvailable"); }
    private boolean canWrite() { return resumed && live && !commandPending && snapshot != null && snapshot.optBoolean("canWrite") && section("settings").optString("revision").matches("[0-9a-f]{32}"); }
    private void paint(boolean preserve) {
        if (connection == null) return; connection.setText(live ? transportName() + " 已连接" : status); connection.setTextColor(live ? green : muted);
        String key = page + "|" + status + "|" + commandPending + "|" + orientation() + "|" + (snapshot == null ? "" : snapshot.toString()); if (key.equals(displayKey)) return; displayKey = key;
        int position = preserve ? scroll.getScrollY() : scrollY; scrollY = 0; content.removeAllViews(); content.addView(label(PAGES[page], 26, text, true)); gap(content, 7);
        String last = status; if (!live && sampledAt != 0) last += " · 最后收到 " + DateFormat.getTimeInstance(DateFormat.MEDIUM).format(new Date(sampledAt)); note(last);
        if (desktopFailed && hasDesktopPairing()) note("电脑界面暂不可用。确认 Mac 上的 Magpie 和 USB 服务正在运行后重试。");
        if (snapshot == null && page != 3) { note("使用 USB 连接电脑。局域网需在设置中配对。USB 调试已授权后会自动识别。"); content.addView(button("USB 自动连接", true, this::pairDialog), lp(-1, -2)); }
        else if (page == 0) drawAgents(); else if (page == 1) drawGateway(); else if (page == 2) drawRoutes(); else drawSettings();
        scroll.post(() -> scroll.scrollTo(0, position));
    }
    private void drawAgents() {
        JSONArray rows = items("agents"); if (!upstream()) note("电脑没有提供完整状态。请检查 Magpie 是否运行且已启用本机浏览器访问。");
        if (rows == null || rows.length() == 0) { if (upstream()) note("Magpie 暂未报告智能体。"); return; }
        LinearLayout card = panel(); for (int i = 0; i < Math.min(rows.length(), 160); i++) { JSONObject value = rows.optJSONObject(i); if (value == null) continue; String model = value.optString("model"); entry(card, value.optString("name", value.optString("id")), model.isEmpty() ? "未报告模型" : model, muted); }
    }
    private void drawGateway() {
        JSONObject gateway = section("gateway"); boolean reachable = gateway.optBoolean("available"); LinearLayout card = panel(); entry(card, "Magpie 网关", reachable ? "可用" : "不可用", reachable ? green : muted);
        entry(card, "进行中的请求", reachable && !gateway.isNull("inFlight") ? String.valueOf(gateway.optInt("inFlight")) : "未报告", text); note("仅统计经过这台电脑上 Magpie 网关的请求。短于采样间隔的请求可能不会出现在在途计数中。");
    }
    private void drawRoutes() {
        JSONArray rows = items("routing"); if (rows == null) { note("电脑未提供路由摘要。"); return; } if (rows.length() == 0) { note("暂无可显示的路由记录。仅显示已识别模型的有限摘要。"); return; }
        LinearLayout card = panel(); for (int i = 0; i < Math.min(rows.length(), 12); i++) { JSONObject item = rows.optJSONObject(i); if (item == null) continue; String result = item.optString("outcome");
            entry(card, item.optString("model"), "inFlight".equals(result) ? "进行中" : "success".equals(result) ? "成功" : "error".equals(result) ? "失败" : "未知", "success".equals(result) ? green : muted); }
    }
    private void drawSettings() {
        if (hasDesktopPairing()) {
            content.addView(button("打开电脑界面", true, this::openDesktop), lp(-1, -2)); gap(content, 12);
        }
        JSONObject settings = section("settings"); LinearLayout card = panel(); int index = currencyIndex(settings.optString("currency")); entry(card, "电脑用量货币", index >= 0 ? CURRENCY_NAMES[index] : "未报告", text);
        if (canWrite()) card.addView(button("更改电脑用量货币", true, this::currencyDialog), lp(-1, -2)); else note(commandPending ? "正在等待电脑确认。" : "要更改货币，请在电脑端以允许写入模式启动同步桥。");
        LinearLayout device = panel(); device.addView(button("屏幕方向 · " + ORIENTATIONS[orientation()], false, this::orientationDialog), lp(-1, -2)); divider(device); device.addView(button(paired == null ? "USB 未连接" : "管理局域网配对", false, this::pairDialog), lp(-1, -2));
    }
    private void orientationDialog() {
        activeDialog = new AlertDialog.Builder(this).setTitle("屏幕方向").setSingleChoiceItems(ORIENTATIONS, orientation(), (dialog, which) -> { getPreferences(MODE_PRIVATE).edit().putInt("orientation", which).apply(); dialog.dismiss(); activeDialog = null; applyOrientation(); paint(true); })
                .setNegativeButton("取消", null).create(); activeDialog.setOnDismissListener(dialog -> activeDialog = null); activeDialog.show();
    }
    private void pairDialog() {
        EditText address = new EditText(this); address.setHint("USB 自动识别 · 局域网: https://192.168.x.x:43425"); address.setSingleLine(true);
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS); address.setText(pairedOrigin); address.setSaveEnabled(false); address.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        EditText input = new EditText(this); input.setHint("局域网电脑配对码（USB 无需输入）"); input.setSingleLine(true); input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        input.setSaveEnabled(false); input.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO); LinearLayout holder = column(); padding(holder, 20, 6); holder.addView(address, lp(-1, -2)); holder.addView(input, lp(-1, -2));
        AlertDialog.Builder builder = new AlertDialog.Builder(this).setTitle("局域网配对").setMessage("USB 在 Mac 上授权后自动连接，无需手工配对。此处仅用于电脑不在同一 USB 环境时的私有局域网配对。")
                .setView(holder).setPositiveButton("配对局域网电脑", null).setNegativeButton("关闭", null);
        if (paired != null && !pairedOrigin.contains("127.0.0.1")) builder.setNeutralButton("解除配对", (d, w) -> { PairingStore.clear(this); paired = null; pairedOrigin = "https://127.0.0.1:43425"; snapshot = null; sampledAt = 0; displayKey = ""; connect(); });
        activeDialog = builder.create(); activeDialog.setOnDismissListener(d -> activeDialog = null); activeDialog.show();
        activeDialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> { try { String origin = address.getText().toString().trim(), code = input.getText().toString().trim(); new BridgeClient(code, origin); PairingStore.save(this, code, origin); input.getText().clear(); activeDialog.dismiss(); paired = new BridgeClient(code, origin); pairedOrigin = origin; snapshot = null; sampledAt = 0; displayKey = ""; connect(); }
            catch (IllegalArgumentException e) { input.setError("地址或配对码不正确。请使用 HTTPS 和可信私网地址。"); } catch (Exception e) { input.setError("无法安全保存配对。请检查设备安全芯片并重试。"); } });
    }
    private int currencyIndex(String value) { for (int i = 0; i < CURRENCIES.length; i++) if (CURRENCIES[i].equals(value)) return i; return -1; }
    private void currencyDialog() {
        if (!canWrite()) return; JSONObject settings = section("settings"); final String revision = settings.optString("revision"); final BridgeClient chosen = paired; int current = currencyIndex(settings.optString("currency")); final int[] selection = {current};
        activeDialog = new AlertDialog.Builder(this).setTitle("更改电脑用量货币").setSingleChoiceItems(CURRENCY_NAMES, current, (d, which) -> selection[0] = which).setPositiveButton("保存更改", (d, w) -> { if (selection[0] >= 0 && selection[0] != current && chosen == paired && canWrite()) writeCurrency(chosen, CURRENCIES[selection[0]], revision); }).setNegativeButton("取消", null).create(); activeDialog.setOnDismissListener(d -> activeDialog = null); activeDialog.show();
    }
    private void writeCurrency(BridgeClient client, String value, String revision) {
        commandPending = true; paint(true); new Thread(() -> { int result; try { result = client.setCurrency(value, revision); } catch (Exception e) { result = 0; } final int code = result;
            ui.post(() -> { commandPending = false; if (paired != client || !active) return; paint(true); String message = code == 200 ? "电脑已确认。界面会随下一次状态更新。" : code == 409 ? "电脑上的设置已更新。请等待同步后重新选择，未覆盖电脑设置。" : code == 403 ? "电脑未开启写权限。" : "未能确认结果。请检查电脑设置，APP 不会自动重试。";
                if (!resumed) return; activeDialog = new AlertDialog.Builder(this).setTitle("电脑用量货币").setMessage(message).setPositiveButton("好", null).create(); activeDialog.setOnDismissListener(d -> activeDialog = null); activeDialog.show(); }); }, "magpie-command").start();
    }
}
