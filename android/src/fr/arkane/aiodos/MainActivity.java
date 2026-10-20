package fr.arkane.aiodos;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.Gravity;
import android.view.View;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;

/**
 * AIODOS sur le téléphone : l'interface du nœud AIODOS de ce téléphone, en plein écran.
 *
 * Le nœud tourne dans l'appli elle-même (NodeService, Python par Chaquopy) : l'ouvrir le
 * démarre au besoin, la fermer le laisse tourner en arrière-plan, avec sa notification.
 * Rien d'autre à installer (avant : Termux, docs/android.md).
 */
public class MainActivity extends Activity {
    private static final int NOTIFICATIONS = 1;
    private static final int FILES = 2;
    private static final long SLOW = 15_000;          // première ouverture : mise en place
    private static final long TOO_LONG = 180_000;     // jamais d'attente sans fin

    private final Handler handler = new Handler(Looper.getMainLooper());
    private WebView web;
    private TextView status;
    private String shown;
    private long since;
    private ValueCallback<Uri[]> picking;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(8, 12, 26));
        root.setFitsSystemWindows(true);
        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        web.setBackgroundColor(Color.rgb(8, 12, 26));
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                return pickFiles(callback, params);
            }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                String host = uri.getHost();
                if ("127.0.0.1".equals(host) || "localhost".equals(host)) {
                    return false;                     // l'interface du nœud, ici
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));   // le reste : navigateur
                } catch (ActivityNotFoundException ignored) {
                    // aucune appli pour ce lien
                }
                return true;
            }
        });
        web.setVisibility(View.GONE);
        status = new TextView(this);
        status.setTextColor(Color.rgb(232, 236, 251));
        status.setTextSize(17);
        status.setGravity(Gravity.CENTER);
        status.setPadding(48, 48, 48, 48);
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        root.addView(status, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);
        askNotifications();
        begin();
    }

    /** Android 13 et plus : sans cette autorisation, la notification du nœud n'apparaît que
     *  dans le gestionnaire de tâches ; le nœud tourne quand même. */
    private void askNotifications() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS},
                NOTIFICATIONS);
        }
    }

    private void begin() {
        status.setVisibility(View.VISIBLE);
        web.setVisibility(View.GONE);
        status.setOnClickListener(null);
        status.setText("Démarrage d'AIODOS…");
        NodeService.start(this);
        since = SystemClock.elapsedRealtime();
        handler.post(this::poll);
    }

    private void poll() {
        if (isFinishing()) {
            return;
        }
        if (NodeService.url != null) {
            show(NodeService.url);
            return;
        }
        if (NodeService.error != null) {
            problem("AIODOS n'a pas démarré : " + NodeService.error);
            return;
        }
        long waited = SystemClock.elapsedRealtime() - since;
        if (waited > TOO_LONG) {
            problem("AIODOS ne répond pas après 3 minutes.");
            return;
        }
        if (waited > SLOW) {
            status.setText("Démarrage d'AIODOS…\n\nÀ la première ouverture, l'appli met en "
                + "place Python et ses bibliothèques : jusqu'à une minute.");
        }
        handler.postDelayed(this::poll, 250);
    }

    private void show(String address) {
        status.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        if (!address.equals(shown)) {
            shown = address;
            web.loadUrl(address);
        }
    }

    private void problem(String text) {
        web.setVisibility(View.GONE);
        status.setVisibility(View.VISIBLE);
        status.setText(text + "\n\nToucher pour redémarrer AIODOS.");
        status.setOnClickListener(v -> Relaunch.app(this));
    }

    /** Joindre des fichiers (bibliothèques, conversations) : le sélecteur d'Android. */
    private boolean pickFiles(ValueCallback<Uri[]> callback, WebChromeClient.FileChooserParams params) {
        if (picking != null) {
            picking.onReceiveValue(null);
        }
        picking = callback;
        Intent intent = params.createIntent();
        if (params.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE) {
            intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        }
        try {
            startActivityForResult(intent, FILES);
            return true;
        } catch (ActivityNotFoundException e) {
            picking = null;
            return false;
        }
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        if (request != FILES || picking == null) {
            super.onActivityResult(request, result, data);
            return;
        }
        Uri[] chosen = null;
        if (result == RESULT_OK && data != null) {
            ClipData clip = data.getClipData();
            if (clip != null) {
                chosen = new Uri[clip.getItemCount()];
                for (int i = 0; i < clip.getItemCount(); i++) {
                    chosen[i] = clip.getItemAt(i).getUri();
                }
            } else if (data.getData() != null) {
                chosen = new Uri[] {data.getData()};
            }
        }
        picking.onReceiveValue(chosen);
        picking = null;
    }

    @Override
    public void onBackPressed() {
        if (web.getVisibility() == View.VISIBLE && web.canGoBack()) {
            web.goBack();
        } else {
            moveTaskToBack(true);       // l'appli passe derrière, le nœud continue
        }
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        super.onDestroy();
    }
}
