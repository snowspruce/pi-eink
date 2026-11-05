package ink.eink.kiosk;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * One WebView, nothing else.
 *
 * The Kindle's stock browser (AOSP Browser 4.4.2) keeps a stale 89px content offset after
 * it collapses its quick-controls toolbar - a black band over the page, and the input box
 * ends up under the soft keyboard. Both are that app's UI; the engine underneath is the
 * same Chromium 30 WebView any app on this ROM gets. So this app is the whole UI:
 *
 *   - no title bar, no toolbar, fullscreen window -> nothing to collapse, nothing to leave
 *     behind, no band
 *   - windowSoftInputMode=adjustResize (in the manifest) -> the keyboard shrinks the
 *     viewport, the page scrolls its own box into view
 *   - BACK reloads the page instead of leaving it: a kiosk should not be able to walk out
 *     of the page it was started for
 */
public class Kiosk extends Activity {
    /** Used when no URL is passed in; the launcher's start command passes one anyway. */
    private static final String HOME = "https://eink.blueberry.cafe/";

    private WebView web;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);

        // No title bar, but NOT fullscreen: a fullscreen window is not resized for the
        // soft keyboard (the IME just covers it), and the input box then sits under the
        // keyboard with no way for the page to know. Measured on this device: fullscreen
        // kept the WebView at 1356px with mInputShown=true; non-fullscreen shrinks it.
        // Costs the 44px status bar - worth it for a working keyboard.
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        // White, not the theme's dark background: an e-ink screen flashes black otherwise.
        getWindow().setBackgroundDrawable(new ColorDrawable(Color.WHITE));

        web = new WebView(this);
        web.setBackgroundColor(Color.WHITE);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        // The page is laid out for 511 CSS px; letting the WebView re-scale it (or the
        // text-inflation the stock browser applies) is what makes a Kindle page unreadable.
        s.setUseWideViewPort(false);
        s.setLoadWithOverviewMode(false);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setTextZoom(100);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);

        web.setWebViewClient(new WebViewClient());

        String url = HOME;
        if (getIntent() != null && getIntent().getDataString() != null) {
            url = getIntent().getDataString();
        }
        web.loadUrl(url);
        setContentView(web);
    }

    /** singleTask: a second `am start` reuses this instance, so take the new URL from it. */
    @Override
    protected void onNewIntent(android.content.Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (intent != null && intent.getDataString() != null && web != null) {
            web.loadUrl(intent.getDataString());
        }
    }

    @Override
    public void onBackPressed() {
        // Never leave: the device is mounted on a wall, the page is the product.
        web.reload();
    }

    @Override
    public boolean onKeyDown(int code, KeyEvent event) {
        if (code == KeyEvent.KEYCODE_BACK) {
            web.reload();
            return true;
        }
        return super.onKeyDown(code, event);
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (web != null) {
            web.onPause();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) {
            web.onResume();
        }
    }
}
