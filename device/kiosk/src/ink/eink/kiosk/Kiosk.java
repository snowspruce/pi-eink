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
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                painted = false;
                noticeIfHanging(url);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                painted = true;
                // Reset the retry ladder only for a real network page. The "connecting /
                // cannot reach" notice is itself a page (loadDataWithBaseURL, url null or
                // about:blank) and it finishes right after every failure: resetting there
                // pinned the retry at 20 s forever, which is what the first test showed.
                if (url != null && url.startsWith("http")) {
                    retryStep = 0;
                }
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
                    // Both paths are down (the VPS itself was unreachable once: ping,
                    // 443 and 2222 all timed out from the Mac). A blank white page says
                    // nothing, so say what failed and keep retrying quietly.
                    showOffline(url, desc);
                    return;
                }
                if (url != null && url.startsWith("https://")) {
                    foundThisLaunch = true;
                    showOffline(url, desc);
                    return;         // already on the public path: nothing left to try
                }
                foundThisLaunch = true;
                String pub = PUBLIC.replaceAll("/+$", "") + "/home";
                Log.i(TAG, "falling back to " + pub);
                load(withToken(pub));
            }

            /**
             * Nothing answered. The device is a reader, not a browser: a blank page is the
             * worst possible answer. Name the target and what the engine said, and retry on
             * its own - the link comes back (a tunnel restarts, a VPS returns) without anyone
             * touching the Kindle.
             */
            private void showOffline(final String url, String desc) {
                // Never print the token: `k=` is the master credential, and this page sits
                // on an e-ink screen in the room. Only scheme+host are useful to the reader.
                String shown = url == null ? "" : url;
                int cut = shown.indexOf('?');
                if (cut > 0) {
                    shown = shown.substring(0, cut);
                }
                showMessage(tr("连不上", "Cannot reach it"), shown,
                        desc + tr("。每 20 秒自己重试一次；也可以按 ⋮ → Address 手输地址。",
                                  ". Retrying every 20 s; or use ⋮ → Address."));
                // Retry the address that failed, not this placeholder: reload() would
                // reload the message page and nothing would ever come back. Back off
                // 20 s -> 1 min -> 5 min so a dead network does not wake the radio
                // every 20 seconds forever; onPageFinished resets the ladder.
                final long wait = RETRY_MS[Math.min(retryStep, RETRY_MS.length - 1)];
                retryStep += 1;
                web.postDelayed(new Runnable() {
                    public void run() {
                        if (!isFinishing() && url != null && url.length() > 0) {
                            load(url);
                        }
                    }
                }, wait);
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
                boolean ourHost = isOurHost(url);
                boolean ours = issuer.toLowerCase().contains("let's encrypt")
                        || issuer.toLowerCase().contains("lets encrypt");
                boolean hostMatches = cn.length() > 0 && url.contains(cn);
                Log.i(TAG, "ssl " + error.getPrimaryError() + " issuer=" + issuer
                        + " cn=" + cn + " url=" + url);
                if (!ourHost) {
                    // Someone else's page (a hotel/cafe login form). Our token is never
                    // sent there, and refusing made logging in impossible: the portal's
                    // own https page hit this same branch and the reader was stuck.
                    Log.i(TAG, "ssl tolerated for foreign host " + url);
                    handler.proceed();
                    return;
                }
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
                // Default is the public entry. LAN is opt-in (⋮ -> This network, or a
                // typed/pushed LAN address), because a LAN address that happens to be
                // remembered from another network is a blank screen, and hunting the
                // local /24 on every start made the device depend on one Wi-Fi.
                url = PUBLIC.replaceAll("/+$", "") + "/home";
            }
            url = withToken(url);
        }
        if (url == null || url.length() == 0) {
            url = withToken(PUBLIC.replaceAll("/+$", "") + "/home");
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
        // A hotel/airport/cafe hotspot answers TLS with its own certificate (Aruba, Cisco,
        // Fortinet...) until the reader logs in. Refusing is the whole point - proceeding
        // would hand the token to whoever runs the hotspot - but "certificate untrusted"
        // alone left the reader on a white page with nothing to do. Name the likely cause
        // and offer the one action that fixes it: open the portal over plain http.
        String host = "";
        try {
            android.net.Uri u = android.net.Uri.parse(url == null ? "" : url);
            host = u.getHost() == null ? "" : u.getHost();
        } catch (Exception ignored) {
            host = "";
        }
        final String portal = host.length() == 0 ? "" : "http://" + host + "/";
        runOnUiThread(new Runnable() {
            public void run() {
                new AlertDialog.Builder(Kiosk.this)
                        .setTitle(tr("证书不是这台机器的，已停止加载", "Not this machine's certificate"))
                        .setMessage(tr(
                                "签发者: ", "Issuer: ") + issuer + "\n"
                                + tr("证书名: ", "Subject: ") + cn + "\n"
                                + tr("地址: ", "Address: ") + (portal.length() > 0 ? portal : url) + "\n\n"
                                + tr("多半是这个网络（酒店/机场/咖啡）的登录页在中间拦截。"
                                     + "token 不会交给它。",
                                     "Most likely this network's login page is intercepting TLS. "
                                     + "The token is not handed over.")
                                + (portal.length() > 0
                                   ? tr("\n\n先在这个网络上登录，再回来重试。", "\n\nLog in to this network first, then retry.")
                                   : ""))
                        .setPositiveButton(tr("打开登录页", "Open login page"),
                                portal.length() == 0 ? null : new DialogInterface.OnClickListener() {
                                    public void onClick(DialogInterface d, int which) {
                                        // plain http, no token: the portal answers it
                                        showMessage(tr("打开登录页", "Opening the login page"), portal,
                                                tr("如果出现登录页，登录完按 ⋮ → Home 回来。",
                                                   "If a login page appears, sign in and use ⋮ → Home."));
                                        web.loadUrl(portal);
                                    }
                                })
                        .setNegativeButton(tr("知道了", "OK"), null)
                        .show();
            }
        });
    }

    /** Cookies that are per origin but whose *value* is one setting the reader made once.
     *
     *  The OTP session, the font size and the page language all live in cookies, so the LAN
     *  address and the public address each keep their own copy: enter the code at home and
     *  travel, and the code is asked for again; pick English on the public entry and the
     *  LAN page comes back in Chinese (buttons said Review/Run in one and 审查/执行 in the
     *  other). Remember each value and plant it on every origin we load.
     */
    private static final String[] SHARED_COOKIES = {"pieink_otp", "piefs", "pielang"};

    private void plantOtpCookie(String url) {
        if (!isOurHost(url)) {
            return;
        }
        try {
            CookieManager cm = CookieManager.getInstance();
            String have = cm.getCookie(url);
            for (String name : SHARED_COOKIES) {
                String saved = prefs.getString("cookie:" + name, "");
                if (saved.length() == 0) {
                    continue;
                }
                if (have != null && have.contains(name + "=")) {
                    continue;
                }
                cm.setCookie(url, name + "=" + saved + "; path=/");
            }
        } catch (Exception ignored) {
            // cookie store unavailable: worst case the value is picked again
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
                for (String name : SHARED_COOKIES) {
                    if (!part.startsWith(name + "=")) {
                        continue;
                    }
                    String value = part.substring(name.length() + 1);
                    if (value.length() == 0) {
                        continue;
                    }
                    String key = "cookie:" + name;
                    if (!value.equals(prefs.getString(key, ""))) {
                        prefs.edit().putString(key, value).commit();
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
        if (!isOurHost(url)) {
            return;                       // never hand the token to a foreign origin
        }
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

    /** Set once a page has actually finished: a hung connect must not look like a hang. */
    private boolean painted = true;

    /** Retry ladder for a page that could not be reached: 20 s, 1 min, then every 5 min. */
    private static final long[] RETRY_MS = {20000, 60000, 300000};
    private int retryStep = 0;

    /**
     * A blackholed route answers nothing for 30-60 s, and an e-ink screen shows white for
     * all of it. Rather than wait for the engine to give up, say what is being tried.
     */
    /** Shown only after a load has clearly stalled - and it must never be terminal.
     *
     *  `loadDataWithBaseURL` REPLACES the in-flight load, so showing this too eagerly
     *  turns a slow-but-fine page into a permanent "Connecting" (measured: a 7 s
     *  threshold fired on this link, the real page was killed, nothing retried). Two
     *  changes: wait long enough that a working link is never interrupted, and schedule
     *  the same retry ladder so the notice can always recover.
     */
    private void noticeIfHanging(String url) {
        String host = url == null ? "" : url;
        try {
            android.net.Uri u = android.net.Uri.parse(url);
            host = (u.getScheme() == null ? "" : u.getScheme() + "://") + u.getHost()
                    + (u.getPort() > 0 ? ":" + u.getPort() : "");
        } catch (Exception ignored) {
            // keep the raw string: it is only a label
        }
        final String label = host;
        final String target = url;
        web.postDelayed(new Runnable() {
            public void run() {
                if (!painted && !isFinishing() && hasWindowFocus()) {
                    final long wait = RETRY_MS[Math.min(retryStep, RETRY_MS.length - 1)];
                    retryStep += 1;
                    showMessage(tr("连接中", "Connecting"), label,
                            tr("还没通。每 ", "No answer yet. Retrying in ")
                            + (wait / 1000) + tr(" 秒自己再试一次；也可以按 ⋮ → Address 换地址。",
                                                 " s; or use ⋮ → Address with another address."));
                    if (target != null && target.length() > 0) {
                        web.postDelayed(new Runnable() {
                            public void run() {
                                if (!isFinishing()) {
                                    load(target);
                                }
                            }
                        }, wait);
                    }
                }
            }
        }, 20000);
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
    /** True only for the addresses this app is allowed to trust with the token.
     *
     *  A captive portal's page is a foreign site: it must not become "home", must not
     *  receive the token cookie, and its certificate must not be judged by our rule -
     *  otherwise the reader can never finish logging in (the portal's own https page
     *  gets refused, and the portal's origin replaces ours as "home").
     */
    private boolean isOurHost(String url) {
        try {
            android.net.Uri u = android.net.Uri.parse(url == null ? "" : url);
            String host = u.getHost();
            if (host == null || host.length() == 0) {
                return false;
            }
            if (host.equals(android.net.Uri.parse(PUBLIC).getHost())) {
                return true;
            }
            android.net.Uri origin = android.net.Uri.parse(prefs.getString("origin", ""));
            return host.equals(origin.getHost());
        } catch (Exception ignored) {
            return false;
        }
    }

    /** True for addresses that may legitimately become "home": the public entry, loopback
     *  and RFC1918. A cafe's login page, a captive portal IP or a test address is none of
     *  those - it must not overwrite the remembered origin even when the URL carries a
     *  token (withToken adds that itself, so its presence proves nothing). */
    private static boolean mayBecomeHome(String host) {
        if (host == null || host.length() == 0) {
            return false;
        }
        if (host.equals(android.net.Uri.parse(PUBLIC).getHost())) {
            return true;
        }
        if (host.equals("localhost") || host.equals("127.0.0.1")) {
            return true;
        }
        if (host.matches("192\\.168\\.\\d{1,3}\\.\\d{1,3}") || host.matches("10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}")) {
            return true;
        }
        if (host.matches("172\\.(1[6-9]|2\\d|3[01])\\.\\d{1,3}\\.\\d{1,3}")) {
            return true;
        }
        return false;
    }

    private void learnOrigin(String url) {
        if (url == null) {
            return;
        }
        try {
            android.net.Uri u = android.net.Uri.parse(url);
            if (u.getScheme() == null || u.getHost() == null) {
                return;
            }
            // A page someone else served must not become home: the portal login page did
            // exactly that once, and ⋮ → Home then opened the cafe's page.
            if (!mayBecomeHome(u.getHost())) {
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
        // Only hand the token to our own addresses. Appending it to whatever was typed put
        // the master credential in the URL of a cafe's login page (and from there into its
        // logs); the first join of a *new* origin still works, because that address is
        // typed with ?k= or learned from a tokenised URL.
        boolean firstJoin = prefs.getString("origin", "").length() == 0;
        if (!firstJoin && !isOurHost(url)) {
            return url;
        }
        return url + (url.indexOf('?') >= 0 ? "&" : "?") + "k=" + token();
    }

    private void remember(String url) {
        try {
            android.net.Uri u = android.net.Uri.parse(url == null ? "" : url);
            if (!mayBecomeHome(u.getHost())) {
                return;
            }
        } catch (Exception ignored) {
            return;
        }
        learnToken(url);
        if (url == null || url.length() == 0) {
            return;
        }
        prefs.edit().putString(KEY_HOME, url).putString("last", url).commit();
    }

    /** Ask the LAN who serves the page; fall back to the public address. */
    /** LAN on request only (⋮ -> This network). Nothing here runs at startup: the default
     *  entry is the public one, and a search that quietly overrides it is exactly the
     *  "which address am I on" confusion this reader does not need. */
    private void discoverAndOpen() {
        new Thread(new Runnable() {
            public void run() {
                final String found = discover();
                runOnUiThread(new Runnable() {
                    public void run() {
                        Log.i(TAG, "discovery " + (found != null ? "found " + found : "found nothing"));
                        if (found != null) {
                            load(withToken(found.replaceAll("/+$", "") + "/home"));
                        } else {
                            showMessage(tr("没找到这台电脑", "No computer found"),
                                    tr("（在这个网络里搜了广播和本机 /24）",
                                       "(searched this network: broadcast and the local /24)"),
                                    tr("确认 Kindle 和电脑连的是同一个 Wi-Fi；也可以按 ⋮ → Address 直接输地址。",
                                       "Check that the Kindle and the Mac are on the same Wi-Fi; "
                                       + "or type the address with ⋮ → Address."));
                        }
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
                    // A broadcast answers across subnets - the AP forwards it, the HTTP
                    // return path does not exist. Trusting the reply alone loaded
                    // http://192.168.2.222:8000 from a device on 192.168.3.x and showed a
                    // blank page. Prove it answers /whois before believing it.
                    if (probe(host) == null) {
                        Log.i(TAG, "broadcast from " + host + " does not answer, ignoring");
                        continue;
                    }
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

    /** The app's own chrome follows the page: an English page must not pop a Chinese
     *  dialog ("连不上" under "Stop / Continue" reads like a bug). */
    private String tr(String zh, String en) {
        return pageIsEnglish() ? en : zh;
    }

    /** True when the page the reader is looking at is English.
     *
     *  The menu is a native dialog, so it does not follow the page by itself: with the
     *  reader on "EN" the items stayed Chinese. The viewer's `pielang` cookie is the
     *  same switch the page obeys.
     */
    private boolean pageIsEnglish() {
        try {
            String have = CookieManager.getInstance().getCookie(web.getUrl() == null ? "" : web.getUrl());
            if (have == null) {
                return false;
            }
            for (String part : have.split(";")) {
                part = part.trim();
                if (part.equals("pielang=en")) {
                    return true;
                }
            }
        } catch (Exception ignored) {
            // no cookie store: Chinese, the default
        }
        return false;
    }

    /** One page, three lines: what failed, where, and what the reader can do. Never the
     *  token - `k=` is the master credential and this is a screen in a room. */
    private void showMessage(String title, String where, String detail) {
        painted = true;           // this page *is* the answer; do not cover it later
        String html = "<html><head><meta name=\"viewport\" "
                + "content=\"width=device-width, initial-scale=1\"></head>"
                + "<body style=\"font-family:sans-serif;font-size:26px;line-height:1.4;padding:24px\">"
                + "<p><b>" + esc(title) + "</b></p>"
                + "<p style=\"word-break:break-all;font-size:20px\">" + esc(where) + "</p>"
                + "<p style=\"font-size:20px\">" + esc(detail) + "</p></body></html>";
        try {
            // Cancel whatever is still in flight first: feeding loadData to a WebView that
            // is mid-load crashed this Chromium 30 once (Fatal signal 11 in a WebView
            // thread, right after a 42 KB page was requested).
            web.stopLoading();
            web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
        } catch (Exception ignored) {
            // the notice is cosmetic: never let it take the reader's page down
        }
    }

    /** Minimal HTML escape: the store URL and the engine's message are text, not markup. */
    private static String esc(String s) {
        if (s == null) {
            return "";
        }
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    /** ⋮ opens a list of what a kiosk actually needs; typing an address is one of them. */
    private void showMenu() {
        String[] labels = pageIsEnglish()
                ? new String[]{"Home", "Reload", "Font A+", "Font A\u2212", "Address", "Desktop"}
                : new String[]{"主页", "重新加载", "字号 A＋", "字号 A－", "输入地址", "桌面"};
        new AlertDialog.Builder(this)
                .setTitle("pi")
                .setItems(labels,
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
                                        fontStep(2);
                                        break;
                                    case 3:
                                        fontStep(-2);
                                        break;
                                    case 4:
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

    /** 字号：走 viewer 的 /fs，带上当前会话 id，改完回到同一页。 */
    private void fontStep(int step) {
        String now = web.getUrl() == null ? "" : web.getUrl();
        String base = "", sid = null;
        try {
            android.net.Uri u = android.net.Uri.parse(now);
            if (u.getScheme() != null && u.getHost() != null) {
                base = u.getScheme() + "://" + u.getHost() + (u.getPort() > 0 ? ":" + u.getPort() : "");
                sid = u.getQueryParameter("s");
            }
        } catch (Exception ignored) {
            // no current URL: fall back to the home origin below
        }
        if (base.length() == 0) {
            String home = homeUrl();
            if (home.length() == 0) {
                return;
            }
            base = home.substring(0, home.length() - "/home".length());
        }
        String here = "";
        try {
            here = android.net.Uri.parse(now).getPath();
        } catch (Exception ignored) {
            here = "";
        }
        String back = "";
        if (sid == null && here != null && here.length() > 1) {
            back = "&next=" + android.net.Uri.encode(here);
        }
        load(withToken(base + "/fs?d=" + step + (sid != null ? "&s=" + sid : "") + back));
    }

    private void goHome() {
        String home = reachableHome();
        load(withToken(home.length() > 0 ? home : PUBLIC.replaceAll("/+$", "") + "/home"));
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
                .setTitle(tr("地址（⋮ 打开）", "Address (⋮)"))
                .setView(input)
                .setPositiveButton(tr("打开", "Open"), new DialogInterface.OnClickListener() {
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
                .setNeutralButton(pageIsEnglish() ? "This network" : "自动发现",
                        new DialogInterface.OnClickListener() {
                            public void onClick(DialogInterface d, int which) {
                                discoverAndOpen();
                            }
                        })
                .setNegativeButton(tr("取消", "Cancel"), null)
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
