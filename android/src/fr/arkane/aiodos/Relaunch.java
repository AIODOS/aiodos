package fr.arkane.aiodos;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;

/**
 * Redémarrer AIODOS dans un processus neuf (« Toucher pour redémarrer ») : Python ne démarre
 * qu'une fois par processus. Cette activité, sans écran, tourne dans un processus à part
 * (« :relance », AndroidManifest.xml) : elle arrête l'ancien, rouvre l'appli et s'en va.
 */
public class Relaunch extends Activity {
    private static final String OLD = "ancien";

    static void app(Activity from) {
        from.stopService(new Intent(from, NodeService.class));
        from.startActivity(new Intent(from, Relaunch.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            .putExtra(OLD, android.os.Process.myPid()));
        from.finish();
        Runtime.getRuntime().exit(0);
    }

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        int old = getIntent().getIntExtra(OLD, -1);
        if (old > 0) {
            android.os.Process.killProcess(old);
        }
        startActivity(new Intent(this, MainActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK));
        finish();
        Runtime.getRuntime().exit(0);
    }
}
