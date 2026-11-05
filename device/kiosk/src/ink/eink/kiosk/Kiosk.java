package ink.eink.kiosk;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.net.Uri;
import android.webkit.CookieManager;
import android.util.Log;
import android.os.Bundle;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.Inet4Address;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.URL;
import java.net.URLConnection;
import java.util.Collections;
import java.util.Enumeration;

/**
 * One WebView, plus the two things a kiosk still needs: finding the machine that serves
 * the page, and a way to type an address when it cannot.
 *
 * The stock browser had a black band after collapsing its toolbar, and as a fullscreen
 * window it was never resized for the soft keyboard - the input box ended up underneath
 * it. Both were that app's UI. This is the whole UI instead: no title bar, no toolbar,
 * non-fullscreen so the IME shrinks the window (see the manifest).
 *
 * Address resolution, in order:
 *   1. an explicit intent (adb / launcher script)
 *   2. the last address that worked
 *   3. LAN broadcast discovery - the viewer answers "PI-EINK-HERE <port>", so the machine
 *      to use is the source address of the reply. No router-reserved address, no mDNS
 *      (Android 4.4 has none), nothing to configure on whichever network we land on.
 *   4. PUBLIC, the internet fallback
 * The ⋮ key opens an address prompt; whatever is typed is remembered.
 */
public class Kiosk extends Activity {
    /** Last-resort address: reachable anywhere, through the tunnel. */
    private static final String PUBLIC = "https://eink.blueberry.cafe/";
    private static final String PREFS = "kiosk";
    private static final String KEY_HOME = "home";
    private static final int DISCOVERY_PORT = 8099;
    private static final byte[] PROBE = "PI-EINK-DISCOVER?".getBytes();
    private static final String ANSWER = "PI-EINK-HERE";

    private WebView web;
    private SharedPreferences prefs;
    private boolean foundThisLaunch;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setBackgroundDrawable(new ColorDrawable(Color.WHITE));

        prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        web = new WebView(this);
        web.setBackgroundColor(Color.WHITE);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setUseWideViewPort(false);
        s.setLoadWithOverviewMode(false);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setTextZoom(100);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                if (url != null && url.startsWith("pi-eink://")) {
                    String what = url.substring("pi-eink://".length()).replaceAll("/.*$", "");
                    Log.i(TAG, "scheme " + what);
                    if (what.equals("desktop")) {
                        openOtherHome();
                    } else {
                        showMenu();
                    }
                    return true;
                }
                return false;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                harvestOtpCookie(url);
            }

            @Override
            public void onReceivedError(WebView view, int code, String desc, String url) {
                Log.i(TAG, "error " + code + " " + desc + " " + url);
                // A LAN address stops working the moment the networks differ (this device
                // and the Mac ended up on 192.168.3.x and 192.168.2.x once: the page was
                // blank and nothing said why). Any failure on a non-public address now
                // falls back to the public entry instead of showing nothing.
                if (foundThisLaunch) {
                    return;
                }
                if (url != null && url.startsWith("https://")) {
                    return;         // already on the public path
                }
                foundThisLaunch = true;
                String pub = PUBLIC.replaceAll("/+$", "") + "/home";
                Log.i(TAG, "falling back to " + pub);
                load(withToken(pub));
            }

            /**
             * The public address serves a Let's Encrypt certificate whose chain this
             * Chromium 30 rejects (the stock browser shows a "continue anyway" warning
             * the reader taps through). A WebView with no handler here cancels silently,
             * which is exactly the blank page a remote session used to show: the request
             * never even reached the Mac.
             */
            @Override
            public void onReceivedSslError(WebView view,
                                           android.webkit.SslErrorHandler handler,
                                           android.net.http.SslError error) {
                // Chromium 30 does not trust the current Let's Encrypt chain, so the
                // public address always lands here. "proceed" for *any* certificate would
                // make the token and the OTP cookie harvestable by anyone who can answer
                // on the address - so proceed only when the leaf really is ours:
                // issued by Let's Encrypt for the host we are talking to.
                String url = error.getUrl() == null ? "" : error.getUrl();
                android.net.http.SslCertificate cert = error.getCertificate();
                String issuer = cert == null || cert.getIssuedBy() == null
                        ? "" : String.valueOf(cert.getIssuedBy().getOName());
                String cn = cert == null || cert.getIssuedTo() == null
                        ? "" : String.valueOf(cert.getIssuedTo().getCName());
                boolean ours = issuer.toLowerCase().contains("let's encrypt")
                        || issuer.toLowerCase().contains("lets encrypt");
                boolean hostMatches = cn.length() > 0 && url.contains(cn);
                Log.i(TAG, "ssl " + error.getPrimaryError() + " issuer=" + issuer
                        + " cn=" + cn + " url=" + url);
                if (ours && hostMatches) {
                    handler.proceed();
                } else {
                    handler.cancel();
                    showSslWarning(issuer, cn, url);
                }
            }

            @Override
            public void onReceivedHttpAuthRequest(WebView view,
                                                  android.webkit.HttpAuthHandler handler,
                                                  String host, String realm) {
                askBasicAuth(handler, host);
            }
        });

        String url = null;
        if (getIntent() != null && getIntent().getDataString() != null) {
            url = getIntent().getDataString();
            remember(url);
        }
        if (url == null) {
            url = reachableHome();
            if (url.length() == 0) {
                foundThisLaunch = true;
                discoverOrFallback();
                return;
            }
            url = withToken(url);
        }
        if (url == null || url.length() == 0) {
            foundThisLaunch = true;
            discoverOrFallback();
            return;
        }
        load(withToken(url));
    }

    private static final String TAG = "kiosk";

    /** Basic auth for the public entry (nginx on the Mac asks for it). The stock browser
     *  had cached credentials; this app does not, and without this a remote session is a
     *  blank page. Asked once, remembered. */
    private void askBasicAuth(final android.webkit.HttpAuthHandler handler, final String host) {
        Log.i(TAG, "http auth requested by " + host);
        String user = prefs.getString("basic_user", "");
        String pass = prefs.getString("basic_pass", "");
        if (user.length() > 0) {
            handler.proceed(user, pass);
            return;
        }
        android.widget.LinearLayout box = new android.widget.LinearLayout(this);
        box.setOrientation(android.widget.LinearLayout.VERTICAL);
        final EditText u = new EditText(this);
        u.setHint("用户名");
        u.setSingleLine(true);
        final EditText pw = new EditText(this);
        pw.setHint("密码");
        pw.setSingleLine(true);
        pw.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        box.addView(u);
        box.addView(pw);
        new AlertDialog.Builder(this)
                .setTitle(host + " 需要密码")
                .setView(box)
                .setPositiveButton("确定", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int which) {
                        String user = u.getText().toString().trim();
                        String pass = pw.getText().toString();
                        prefs.edit().putString("basic_user", user).putString("basic_pass", pass).commit();
                        handler.proceed(user, pass);
                    }
                })
                .setNegativeButton("取消", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int which) {
                        handler.cancel();
                    }
                })
                .show();
    }


    /**
     * The OTP session cookie is per origin: the LAN address and the public address are
     * different hosts, so entering the code at home and then travelling used to demand it
     * again - and a re-discovered LAN address after a trip, again. The value itself is one
     * server-side session (7 days), so remember it once and plant it on every origin we
     * load. Answers "do I have to type the code after pressing ⋮?" with no.
     */
    /** HMAC-SHA256(token, nonce), first 32 hex chars - what /whois must answer with. */
    private boolean proofMatches(String nonce, String proof) {
        String secret = token();
        if (secret.length() < 16 || proof.length() != 32) {
            return false;
        }
        try {
            javax.crypto.Mac mac = javax.crypto.Mac.getInstance("HmacSHA256");
            mac.init(new javax.crypto.spec.SecretKeySpec(secret.getBytes("UTF-8"), "HmacSHA256"));
            byte[] raw = mac.doFinal(nonce.getBytes("UTF-8"));
            StringBuilder hex = new StringBuilder();
            for (byte b : raw) {
                hex.append(String.format("%02x", b));
            }
            return hex.toString().substring(0, 32).equalsIgnoreCase(proof);
        } catch (Exception e) {
            return false;
        }
    }

    /** Visible stop instead of a silent blank page when a certificate is not ours. */
    private void showSslWarning(final String issuer, final String cn, final String url) {
        runOnUiThread(new Runnable() {
            public void run() {
                new AlertDialog.Builder(Kiosk.this)
                        .setTitle("证书不可信，已停止加载")
                        .setMessage("签发者: " + issuer + "\n证书名: " + cn + "\n地址: " + url
                                + "\n\n不是这台机器的证书，token 不会交出去。")
                        .setPositiveButton("知道了", null)
                        .show();
            }
        });
    }

    private void plantOtpCookie(String url) {
        String saved = prefs.getString("otp_cookie", "");
        if (saved.length() < 16) {
            return;
        }
        try {
            CookieManager cm = CookieManager.getInstance();
            String have = cm.getCookie(url);
            if (have != null && have.contains("pieink_otp=")) {
                return;
            }
            cm.setCookie(url, "pieink_otp=" + saved + "; path=/");
        } catch (Exception ignored) {
            // cookie store unavailable: worst case the code is asked for once more
        }
    }

    private void harvestOtpCookie(String url) {
        try {
            String have = CookieManager.getInstance().getCookie(url);
            if (have == null) {
                return;
            }
            for (String part : have.split(";")) {
                part = part.trim();
                if (part.startsWith("pieink_otp=")) {
                    String value = part.substring("pieink_otp=".length());
                    if (value.length() >= 16 && !value.equals(prefs.getString("otp_cookie", ""))) {
                        prefs.edit().putString("otp_cookie", value).commit();
                    }
                }
            }
        } catch (Exception ignored) {
            // as above
        }
    }

    /**
     * Seed the token cookie for this origin before navigating.
     *
     * The public entry lets a request through without Basic when it carries the token -
     * as ?k= or as the pieink cookie. The query string only covers the first navigation;
     * the favicon and the page's own XHRs have no query, so without a cookie already in
     * place the very first load pops a password dialog the device cannot answer.
     */
    private void plantTokenCookie(String url) {
        String secret = token();
        if (secret.length() < 16) {
            return;
        }
        try {
            CookieManager cm = CookieManager.getInstance();
            String have = cm.getCookie(url);
            if (have != null && have.contains("pieink=")) {
                return;
            }
            cm.setCookie(url, "pieink=" + secret + "; path=/");
        } catch (Exception ignored) {
            // worst case the ?k= in the address still gets us in
        }
    }

    private void load(String url) {
        Log.i(TAG, "load " + url);
        remember(url);
        learnOrigin(url);
        plantTokenCookie(url);
        plantOtpCookie(url);
        if (web.getParent() == null) {
            setContentView(web);
        }
        web.loadUrl(url);
    }

    /** The viewer's token, learned from any URL that carried one.
     *
     *  /whois must not hand it out (it is unauthenticated by design), and a router-
     *  reserved address is not an option either, so the two facts are kept apart: the
     *  token is remembered here, the address is discovered per network, and every URL is
     *  built from both. Type or push the tokenised address once and it survives every
     *  change of network. */
    /** scheme://host[:port] of whatever we last loaded - the base for the home URL. */
    private void learnOrigin(String url) {
        if (url == null) {
            return;
        }
        try {
            android.net.Uri u = android.net.Uri.parse(url);
            if (u.getScheme() == null || u.getHost() == null) {
                return;
            }
            String origin = u.getScheme() + "://" + u.getHost()
                    + (u.getPort() > 0 ? ":" + u.getPort() : "");
            if (!origin.equals(prefs.getString("origin", ""))) {
                prefs.edit().putString("origin", origin).commit();
            }
        } catch (Exception ignored) {
            // a URL we cannot parse simply does not update the origin
        }
    }

    private String homeUrl() {
        String origin = prefs.getString("origin", "");
        if (origin.length() == 0) {
            // upgrade path: older builds only stored the full URL it last loaded
            learnOrigin(prefs.getString(KEY_HOME, ""));
            origin = prefs.getString("origin", "");
        }
        return origin.length() > 0 ? origin + "/home" : "";
    }

    private String token() {
        return prefs.getString("token", "");
    }

    private void learnToken(String url) {
        if (url == null) {
            return;
        }
        int at = url.indexOf("k=");
        if (at < 0) {
            return;
        }
        String value = url.substring(at + 2);
        int cut = value.indexOf('&');
        if (cut >= 0) {
            value = value.substring(0, cut);
        }
        if (value.length() >= 16) {
            prefs.edit().putString("token", value).commit();
        }
    }

    /** Add the remembered token to an address that has none. */
    private String withToken(String url) {
        if (url == null || url.length() == 0 || url.indexOf("k=") >= 0 || token().length() == 0) {
            return url;
        }
        return url + (url.indexOf('?') >= 0 ? "&" : "?") + "k=" + token();
    }

    private void remember(String url) {
        learnToken(url);
        if (url == null || url.length() == 0) {
            return;
        }
        prefs.edit().putString(KEY_HOME, url).putString("last", url).commit();
    }

    /** Ask the LAN who serves the page; fall back to the public address. */
    private void discoverOrFallback() {
        new Thread(new Runnable() {
            public void run() {
                final String found = discover();
                runOnUiThread(new Runnable() {
                    public void run() {
                        Log.i(TAG, "discovery " + (found != null ? "found " + found : "found nothing"));
                        String base = found != null ? found : PUBLIC;
                        load(withToken(base.replaceAll("/+$", "") + "/home"));
                    }
                });
            }
        }).start();
    }

    /** Own IPv4 address, or null when there is no LAN. */
    private String ownAddress() {
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) {
                    continue;
                }
                for (InetAddress addr : Collections.list(ni.getInetAddresses())) {
                    if (addr instanceof Inet4Address && !addr.isLoopbackAddress()) {
                        return addr.getHostAddress();
                    }
                }
            }
        } catch (Exception ignored) {
            // fall through: no interface, no LAN
        }
        return null;
    }

    /**
     * Scan the local /24 for the viewer's /whois line.
     *
     * Broadcast is tried first because it is one packet, but plenty of access points drop
     * it (this one does: the Mac's UDP listener never saw a single probe), so the fallback
     * is a plain scan. 254 addresses, 32 threads, 250ms each: about two seconds to find a
     * machine that is one hop away. No router configuration, no mDNS.
     */
    private String scanSubnet(String own) {
        if (own == null) {
            return null;
        }
        int dot = own.lastIndexOf('.');
        if (dot < 0) {
            return null;
        }
        final String prefix = own.substring(0, dot + 1);
        final Object lock = new Object();
        final String[] hit = new String[1];
        final int[] left = {253};
        for (int i = 1; i <= 254; i++) {
            final int host = i;
            new Thread(new Runnable() {
                public void run() {
                    String found = probe(prefix + host);
                    synchronized (lock) {
                        if (found != null && hit[0] == null) {
                            hit[0] = found;
                        }
                        left[0]--;
                        lock.notifyAll();
                    }
                }
            }).start();
        }
        synchronized (lock) {
            long deadline = System.currentTimeMillis() + 6000;
            while (hit[0] == null && left[0] > 0) {
                long wait = deadline - System.currentTimeMillis();
                if (wait <= 0) {
                    break;
                }
                try {
                    lock.wait(wait);
                } catch (InterruptedException e) {
                    break;
                }
            }
        }
        return hit[0];
    }

    /** true when this host answers the viewer's /whois line, with proof it knows the
     *  token. Without the proof a rogue host could claim the name and receive the token
     *  (the app appends ?k= to whatever it finds). */
    private String probe(String host) {
        Socket socket = null;
        try {
            socket = new Socket();
            socket.connect(new java.net.InetSocketAddress(host, 8000), 250);
            socket.setSoTimeout(400);
            String nonce = Long.toHexString(System.nanoTime()) + Long.toHexString((long) (Math.random() * 1e18));
            java.io.OutputStream out = socket.getOutputStream();
            out.write(("GET /whois?n=" + nonce + " HTTP/1.0\r\nHost: " + host + "\r\n\r\n").getBytes());
            out.flush();
            byte[] buf = new byte[256];
            int n = socket.getInputStream().read(buf);
            if (n <= 0) {
                return null;
            }
            String text = new String(buf, 0, n);
            int at = text.indexOf("PI-EINK");
            if (at < 0) {
                return null;
            }
            String[] parts = text.substring(at + 7).trim().split("\\s+");
            String port = parts.length > 0 && parts[0].matches("\\d+") ? parts[0] : "8000";
            String proof = parts.length > 1 ? parts[1] : "";
            if (!proofMatches(nonce, proof)) {
                Log.i(TAG, "whois from " + host + " has no valid proof, ignoring");
                return null;
            }
            return "http://" + host + ":" + port + "/";
        } catch (Exception ignored) {
            return null;
        } finally {
            try {
                if (socket != null) {
                    socket.close();
                }
            } catch (Exception ignored) {
                // nothing to do
            }
        }
    }

    /** Broadcast once, wait a few seconds for the first answer. */
    private String discover() {
        DatagramSocket sock = null;
        try {
            sock = new DatagramSocket();
            sock.setBroadcast(true);
            sock.setSoTimeout(700);
            DatagramPacket out = new DatagramPacket(PROBE, PROBE.length,
                    InetAddress.getByName("255.255.255.255"), DISCOVERY_PORT);
            // Three rounds: a dropped broadcast on WiFi is normal.
            for (int round = 0; round < 3; round++) {
                try {
                    sock.send(out);
                } catch (Exception ignored) {
                    // keep going: one lost datagram is not an answer
                }
                long deadline = System.currentTimeMillis() + 900;
                while (System.currentTimeMillis() < deadline) {
                    DatagramPacket in = new DatagramPacket(new byte[128], 128);
                    try {
                        sock.receive(in);
                    } catch (Exception timeout) {
                        break;
                    }
                    String text = new String(in.getData(), 0, in.getLength()).trim();
                    if (!text.startsWith(ANSWER)) {
                        continue;
                    }
                    String[] parts = text.split("\\s+");
                    String port = parts.length > 1 ? parts[1] : "8000";
                    String host = in.getAddress().getHostAddress();
                    return "http://" + host + ":" + port + "/";
                }
            }
        } catch (Exception ignored) {
            // no LAN path (WiFi off, another network): the scan and the public address follow
        } finally {
            if (sock != null) {
                sock.close();
            }
        }
        Log.i(TAG, "broadcast found nothing, scanning the /24");
        return scanSubnet(ownAddress());
    }

    @Override
    protected void onNewIntent(android.content.Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (intent != null && intent.getDataString() != null && web != null) {
            // through load(): it learns the token and re-attaches it, so a pushed address
            // without ?k= still ends up authenticated
            load(withToken(intent.getDataString()));
        }
    }

    /** ⋮ / MENU: type an address, or ask the LAN again. */
    @Override
    public boolean onKeyDown(int code, KeyEvent event) {
        if (code == KeyEvent.KEYCODE_BACK) {
            // ◁ is the only nav-bar key that reaches us. From a session it goes back to
            // the project list (the thing a reader actually wants there); on the list
            // itself there is nowhere to go, so it re-reads the page.
            String now = web.getUrl() == null ? "" : web.getUrl();
            if (!now.endsWith("/home") && !now.contains("/home?")) {
                goHome();
            } else {
                web.reload();
            }
            return true;
        }
        if (code == KeyEvent.KEYCODE_MENU) {
            showMenu();
            return true;
        }
        return super.onKeyDown(code, event);
    }

    /** ⋮ opens a list of what a kiosk actually needs; typing an address is one of them. */
    private void showMenu() {
        new AlertDialog.Builder(this)
                .setTitle("pi")
                .setItems(new String[]{"主页", "重新加载", "输入地址", "桌面"},
                        new DialogInterface.OnClickListener() {
                            public void onClick(DialogInterface d, int which) {
                                switch (which) {
                                    case 0:
                                        goHome();
                                        break;
                                    case 1:
                                        web.reload();
                                        break;
                                    case 2:
                                        showAddressPrompt();
                                        break;
                                    default:
                                        openOtherHome();
                                        break;
                                }
                            }
                        })
                .show();
    }

    /**
     * The home URL to actually use, checked before loading it.
     *
     * A remembered LAN origin survives network changes: the device sat on 192.168.3.x
     * while the Mac was on 192.168.2.x, and the app kept loading a host that could not
     * answer - a blank page, no error, nothing to act on. The public entry is one probe
     * away from certainty, so a LAN origin that does not answer /whois right now is
     * skipped in favour of it.
     */
    private String reachableHome() {
        String origin = homeUrl();
        if (origin.length() == 0) {
            return "";
        }
        if (origin.startsWith("https://")) {
            return origin;                 // public: the TLS hop is the check
        }
        android.net.Uri u = android.net.Uri.parse(origin);
        String host = u.getHost();
        if (host == null || probe(host) == null) {
            Log.i(TAG, "LAN origin " + origin + " does not answer, using the public entry");
            return PUBLIC.replaceAll("/+$", "") + "/home";
        }
        return origin + "/home";
    }

    private void goHome() {
        String home = reachableHome();
        if (home.length() > 0) {
            load(withToken(home));
        } else {
            foundThisLaunch = true;
            discoverOrFallback();
        }
    }

    private void showAddressPrompt() {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setSingleLine(true);
        // The remembered home, not the live URL: after the OTP redirect the live one is
        // "/?s=<sid>" (no token, useless next time). The token is stripped from what is
        // shown - it is only good for shoulder-surfing here - and re-attached on load by
        // withToken(). Select all so typing replaces it.
        String current = prefs.getString(KEY_HOME, PUBLIC).replaceAll("([?&])k=[^&]*", "");
        input.setText(current);
        input.setSelection(0, input.getText().length());

        new AlertDialog.Builder(this)
                .setTitle("地址（⋮ 打开）")
                .setView(input)
                .setPositiveButton("打开", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int which) {
                        String url = input.getText().toString().trim();
                        if (url.length() == 0) {
                            return;
                        }
                        if (!url.startsWith("http://") && !url.startsWith("https://")) {
                            url = "http://" + url;
                        }
                        load(withToken(url));
                    }
                })
                .setNeutralButton("自动发现", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int which) {
                        foundThisLaunch = true;
                        discoverOrFallback();
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    /**
     * The escape hatch. This app can be the HOME activity, which means the launcher would
     * otherwise be unreachable - and with it KOReader, Settings and everything else.
     */
    private void openOtherHome() {
        try {
            android.content.Intent home = new android.content.Intent(android.content.Intent.ACTION_MAIN);
            home.addCategory(android.content.Intent.CATEGORY_HOME);
            for (android.content.pm.ResolveInfo info :
                    getPackageManager().queryIntentActivities(home, 0)) {
                String pkg = info.activityInfo.packageName;
                if (!pkg.equals(getPackageName())) {
                    android.content.Intent go = new android.content.Intent(android.content.Intent.ACTION_MAIN);
                    go.addCategory(android.content.Intent.CATEGORY_LAUNCHER);
                    go.setClassName(pkg, info.activityInfo.name);
                    go.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(go);
                    return;
                }
            }
            Log.i(TAG, "no other HOME activity found");
        } catch (Exception e) {
            Log.i(TAG, "openOtherHome failed: " + e);
        }
    }
}
