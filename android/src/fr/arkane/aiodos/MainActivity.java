package fr.arkane.aiodos;

import android.app.Activity;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
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
 * Le nœud tourne dans Termux (docs/android.md). L'application le démarre au besoin par
 * l'intention RUN_COMMAND de Termux, qui lui rend l'adresse de l'interface, jeton compris :
 * le nœud l'écrit dans ~/.aiodos-ui (option --ui-file), un fichier que seul Termux peut lire.
 * Première étape de l'application de l'étude (docs/installeurs.md, § 3), qui embarquera
 * ensuite le nœud lui-même.
 */
public class MainActivity extends Activity {
    private static final String TERMUX = "com.termux";
    private static final String PERMISSION = "com.termux.permission.RUN_COMMAND";
    private static final String RESULT = "fr.arkane.aiodos.RESULTAT";
    private static final String HOME = "/data/data/com.termux/files/home";
    private static final String BASH = "/data/data/com.termux/files/usr/bin/bash";

    /** Trouve le nœud en marche (adresse dans ~/.aiodos-ui, qui répond), ou le (re)démarre
     *  avec --ui-file et les options de ~/.aiodos-options (« --internet »…), puis écrit
     *  l'adresse de son interface. SIGTERM pour arrêter l'ancien : lancé en arrière-plan,
     *  le nœud ignore SIGINT (les versions récentes s'arrêtent proprement sur les deux).
     *  « [p]ython3 » : pkill -f lit toute la ligne de commande, et ce script, qui contient
     *  le motif dans son texte, se serait arrêté lui-même. */
    static final String SCRIPT =
        "F=\"$HOME/.aiodos-ui\"\n"
        + "cd \"$HOME/aiodos\" 2>/dev/null || { echo \"AIODOS n'est pas dans ~/aiodos "
        + "(voir docs/android.md).\" >&2; exit 2; }\n"
        + "alive() { [ -s \"$F\" ] && python3 -c 'import sys, urllib.request; "
        + "urllib.request.urlopen(sys.argv[1].split(\"?\")[0], timeout=3)' \"$(cat \"$F\")\" "
        + "2>/dev/null; }\n"
        + "if alive; then cat \"$F\"; exit 0; fi\n"
        + "rm -f \"$F\"\n"
        + "if command -v pgrep >/dev/null && pgrep -f '[p]ython3 -m aiodos' >/dev/null; then\n"
        + "  pkill -TERM -f '[p]ython3 -m aiodos'\n"
        + "  for i in $(seq 1 20); do pgrep -f '[p]ython3 -m aiodos' >/dev/null || break; sleep 1; done\n"
        + "fi\n"
        + "OPTIONS=$(cat \"$HOME/.aiodos-options\" 2>/dev/null)\n"
        + "nohup sh lancer-noeud.sh --ui-file \"$F\" $OPTIONS >\"$HOME/aiodos-noeud.log\" 2>&1 &\n"
        + "for i in $(seq 1 150); do [ -s \"$F\" ] && { cat \"$F\"; exit 0; }; sleep 1; done\n"
        + "echo \"Le nœud AIODOS n'a pas démarré : voir ~/aiodos-noeud.log dans Termux.\" >&2\n"
        + "tail -n 5 \"$HOME/aiodos-noeud.log\" >&2\n"
        + "exit 1\n";

    private WebView web;
    private TextView status;

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
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                String host = uri.getHost();
                if ("127.0.0.1".equals(host) || "localhost".equals(host)) {
                    return false;                     // l'interface du nœud, ici
                }
                startActivity(new Intent(Intent.ACTION_VIEW, uri));   // le reste : navigateur
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
        if (saved != null && saved.getString("url") != null) {
            show(saved.getString("url"));
            return;
        }
        begin();
    }

    private String url;

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        if (url != null) {
            out.putString("url", url);
        }
    }

    private void begin() {
        if (!termuxInstalled()) {
            problem("Termux n'est pas installé : le nœud AIODOS de ce téléphone y tourne "
                + "(voir docs/android.md).");
        } else if (Build.VERSION.SDK_INT >= 23
                && checkSelfPermission(PERMISSION) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {PERMISSION}, 1);
        } else {
            start();
        }
    }

    private boolean termuxInstalled() {
        try {
            getPackageManager().getPackageInfo(TERMUX, 0);
            return true;
        } catch (PackageManager.NameNotFoundException e) {
            return false;
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            start();
        } else {
            problem("Sans l'autorisation d'exécuter des commandes dans Termux, AIODOS ne peut "
                + "pas démarrer son nœud. Paramètres → Applications → AIODOS → Autorisations.");
        }
    }

    private void start() {
        status.setText("Démarrage du nœud AIODOS…");
        Intent run = new Intent("com.termux.RUN_COMMAND");
        run.setClassName(TERMUX, "com.termux.app.RunCommandService");
        run.putExtra("com.termux.RUN_COMMAND_PATH", BASH);
        run.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", new String[] {"-c", SCRIPT});
        run.putExtra("com.termux.RUN_COMMAND_WORKDIR", HOME);
        run.putExtra("com.termux.RUN_COMMAND_BACKGROUND", true);
        Intent back = new Intent(this, MainActivity.class).setAction(RESULT)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
            | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
        run.putExtra("com.termux.RUN_COMMAND_PENDING_INTENT",
            PendingIntent.getActivity(this, 1, back, flags));
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                startForegroundService(run);
            } else {
                startService(run);
            }
        } catch (RuntimeException e) {
            problem("Termux refuse la commande (" + e.getMessage() + "). Dans Termux : "
                + "echo allow-external-apps=true >> ~/.termux/termux.properties");
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (!RESULT.equals(intent.getAction())) {
            return;
        }
        Bundle result = intent.getBundleExtra("result");
        String out = result == null ? "" : result.getString("stdout", "");
        String err = result == null ? "" : result.getString("stderr", "");
        String message = result == null ? "" : result.getString("errmsg", "");
        for (String line : out.split("\n")) {
            if (line.trim().startsWith("http://127.0.0.1")) {
                show(line.trim());
                return;
            }
        }
        String detail = (err + "\n" + message).trim();
        if (detail.isEmpty() && result != null) {      // rien de lisible : ce que Termux a rendu
            StringBuilder raw = new StringBuilder("Termux n'a rien écrit");
            for (String key : new String[] {"exitCode", "err"}) {
                if (result.containsKey(key)) {
                    raw.append(" · ").append(key).append(" ").append(result.get(key));
                }
            }
            detail = raw.append('.').toString();
        }
        if (detail.contains("allow-external-apps")) {
            detail = "Termux n'accepte pas encore les commandes des autres applications. Dans "
                + "Termux : echo allow-external-apps=true >> ~/.termux/termux.properties";
        }
        problem(detail.isEmpty() ? "Le nœud AIODOS n'a pas répondu." : detail);
    }

    private void show(String address) {
        url = address;
        status.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        web.loadUrl(address);
    }

    private void problem(String text) {
        web.setVisibility(View.GONE);
        status.setVisibility(View.VISIBLE);
        status.setText(text + "\n\nToucher pour réessayer.");
        status.setOnClickListener(v -> {
            status.setOnClickListener(null);
            begin();
        });
    }

    @Override
    public void onBackPressed() {
        if (web.getVisibility() == View.VISIBLE && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
