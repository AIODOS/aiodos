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
import android.widget.Toast;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

/**
 * AIODOS sur le téléphone : l'interface du nœud AIODOS de ce téléphone, en plein écran.
 *
 * Le nœud tourne dans l'appli elle-même (NodeService, Python par Chaquopy) : l'ouvrir le
 * démarre au besoin, la fermer le laisse tourner en arrière-plan, avec sa notification.
 * Rien d'autre à installer (avant : Termux, docs/android.md). Ses textes : res/values*,
 * dans la langue choisie dans les Réglages d'AIODOS, sinon celle du téléphone (Texts).
 */
public class MainActivity extends Activity {
    private static final int NOTIFICATIONS = 1;
    private static final int FILES = 2;
    private static final long SLOW = 15_000;          // première ouverture : mise en place
    private static final long TOO_LONG = 180_000;     // au-delà : on le dit, et on attend encore
    /** Ce qu'un courriel garde (plainMail), comme dans l'interface (aiodos/render.py). */
    static final Set<String> MAIL_FIELDS =
        new HashSet<>(Arrays.asList("to", "cc", "subject", "body"));

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
        root.setBackgroundColor(Color.rgb(6, 8, 7));
        root.setFitsSystemWindows(true);
        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        web.setBackgroundColor(Color.rgb(6, 8, 7));
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
                if (sameOrigin(uri, shown)) {
                    return false;                     // l'interface du nœud, ici
                }
                if (!opensOutside(uri)) {                // ni le web ni un courriel : rien
                    tell(Texts.get(MainActivity.this, R.string.link_refused));
                    return true;
                }
                if ("mailto".equalsIgnoreCase(uri.getScheme())) {
                    uri = Uri.parse(plainMail(uri.toString()));
                }
                Intent intent = new Intent(Intent.ACTION_VIEW, uri);   // le navigateur
                intent.addCategory(Intent.CATEGORY_BROWSABLE);
                try {
                    startActivity(intent);
                } catch (ActivityNotFoundException e) {
                    tell(Texts.get(MainActivity.this, R.string.link_no_app));
                }
                return true;
            }
        });
        web.setVisibility(View.GONE);
        status = new TextView(this);
        status.setTextColor(Color.rgb(229, 239, 235));
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
        status.setText(Texts.get(this, R.string.startup));
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
            problem(Texts.get(this, R.string.node_failed, NodeService.error));
            return;
        }
        long waited = SystemClock.elapsedRealtime() - since;
        if (waited > TOO_LONG) {           // on continue d'attendre : il peut encore venir
            status.setText(Texts.get(this, R.string.startup_too_long));
        } else if (waited > SLOW) {
            status.setText(Texts.get(this, R.string.startup_first));
        }
        handler.postDelayed(this::poll, 250);
    }

    /** Même schéma, même hôte et même port que l'interface du nœud : un autre service de
     *  127.0.0.1 (llama-server, le compagnon…) n'a rien à faire dans cette fenêtre. */
    static boolean sameOrigin(Uri uri, String address) {
        if (address == null || uri == null) {
            return false;
        }
        Uri node = Uri.parse(address);
        return "http".equals(uri.getScheme()) && node.getHost() != null
            && node.getHost().equals(uri.getHost()) && node.getPort() == uri.getPort();
    }

    /** Ce qu'un lien peut ouvrir hors de l'appli : une page web (le navigateur) ou un
     *  courriel, comme dans l'interface (aiodos/render.py). Un lien d'une réponse vient
     *  parfois d'un ami ou d'une page piégée : intent:, market:, tel:, le schéma d'une autre
     *  appli… lanceraient celle-ci sans rien demander. CATEGORY_BROWSABLE : seules les
     *  applis qui acceptent d'être ouvertes depuis un navigateur sont candidates. */
    static boolean opensOutside(Uri uri) {
        String scheme = uri == null ? null : uri.getScheme();
        if (scheme == null) {
            return false;
        }
        scheme = scheme.toLowerCase(Locale.ROOT);
        return scheme.equals("http") || scheme.equals("https") || scheme.equals("mailto");
    }

    /** Un courriel réduit à ses destinataires, son sujet et son texte, comme dans l'interface
     *  (aiodos/render.py, plain_mail) : certaines messageries ont joint d'office le fichier
     *  nommé par attach=… ou attachment=…, un lien piégé ferait partir un fichier du
     *  téléphone. Un nom de champ encodé (%61ttach) n'est pas reconnu, donc écarté. Le
     *  fragment (#…) tombe aussi : rien de ce qui suit l'adresse n'échappe au tri. */
    static String plainMail(String address) {
        int hash = address.indexOf('#');
        if (hash >= 0) {
            address = address.substring(0, hash);
        }
        int ask = address.indexOf('?');
        if (ask < 0) {
            return address;
        }
        StringBuilder kept = new StringBuilder(address.substring(0, ask));
        char glue = '?';
        for (String field : address.substring(ask + 1).split("&")) {
            String name = field.split("=", 2)[0].trim().toLowerCase(Locale.ROOT);
            if (MAIL_FIELDS.contains(name)) {
                kept.append(glue).append(field);
                glue = '&';
            }
        }
        return kept.toString();
    }

    private void tell(String text) {
        Toast.makeText(this, text, Toast.LENGTH_LONG).show();
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
        status.setText(Texts.get(this, R.string.tap_to_restart, text));
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
