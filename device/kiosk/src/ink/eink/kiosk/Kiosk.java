package ink.eink.kiosk;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.net.Uri;
import android.util.Log;
import android.os.Bundle;
import android.text.InputType;
import android.view.KeyEvent;
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
            public void onReceivedError(WebView view, int code, String desc, String url) {
                // A remembered LAN address stops working the moment the network changes.
                // Try discovery once per launch, then fall back to the public address.
                if (foundThisLaunch) {
                    return;
                }
                String home = prefs.getString(KEY_HOME, "");
                if (url != null && url.equals(home) && !home.startsWith("https://")) {
                    foundThisLaunch = true;
                    discoverOrFallback();
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
            url = withToken(prefs.getString(KEY_HOME, ""));
        }
        if (url == null || url.length() == 0) {
            foundThisLaunch = true;
            discoverOrFallback();
            return;
        }
        load(url);
    }

    private static final String TAG = "kiosk";

    /** Basic auth for the public entry (nginx on the Mac asks for it). The stock browser
     *  had cached credentials; this app does not, and without this a remote session is a
     *  blank page. Asked once, remembered. */
    private void askBasicAuth(final android.webkit.HttpAuthHandler handler, final String host) {
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


    private void load(String url) {
        Log.i(TAG, "load " + url);
        remember(url);
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
        if (url == null || url.indexOf("k=") >= 0 || token().length() == 0) {
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
                        load(withToken(found != null ? found : PUBLIC));
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

    /** true when this host answers the viewer's /whois line. */
    private static String probe(String host) {
        Socket socket = null;
        try {
            socket = new Socket();
            socket.connect(new java.net.InetSocketAddress(host, 8000), 250);
            socket.setSoTimeout(400);
            java.io.OutputStream out = socket.getOutputStream();
            out.write(("GET /whois HTTP/1.0\r\nHost: " + host + "\r\n\r\n").getBytes());
            out.flush();
            byte[] buf = new byte[256];
            int n = socket.getInputStream().read(buf);
            if (n <= 0) {
                return null;
            }
            String text = new String(buf, 0, n);
            if (!text.contains("PI-EINK")) {
                return null;
            }
            String port = "8000";
            int at = text.indexOf("PI-EINK");
            String tail = text.substring(at + 7).trim();
            if (tail.length() > 0 && tail.split("\\s")[0].matches("\\d+")) {
                port = tail.split("\\s")[0];
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
            remember(intent.getDataString());
            web.loadUrl(intent.getDataString());
        }
    }

    /** ⋮ / MENU: type an address, or ask the LAN again. */
    @Override
    public boolean onKeyDown(int code, KeyEvent event) {
        if (code == KeyEvent.KEYCODE_BACK) {
            web.reload();
            return true;
        }
        if (code == KeyEvent.KEYCODE_MENU) {
            showAddressPrompt();
            return true;
        }
        return super.onKeyDown(code, event);
    }

    private void showAddressPrompt() {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setSingleLine(true);
        String current = web.getUrl() != null ? web.getUrl() : prefs.getString(KEY_HOME, PUBLIC);
        input.setText(current);
        input.setSelection(input.getText().length());

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
                        remember(url);
                        web.loadUrl(url);
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
}
