package fr.arkane.aiodos;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

/**
 * Démarrage du téléphone : le nœud AIODOS démarre dans Termux, sans ouvrir l'appli, si
 * l'utilisateur l'a demandé (« Démarrer avec le téléphone », dans Cette machine : fichier
 * ~/.aiodos-autostart, aiodos/node/autostart.py). Même script que l'appli (le nœud déjà en
 * marche est gardé), lancé en arrière-plan par l'intention RUN_COMMAND de Termux. Android
 * permet de démarrer un service au premier plan juste après le démarrage.
 */
public class BootReceiver extends BroadcastReceiver {
    static final String ONLY_IF_ASKED = "[ -f \"$HOME/.aiodos-autostart\" ] || exit 0\n";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action)
                && !"android.intent.action.QUICKBOOT_POWERON".equals(action)) {
            return;
        }
        Intent run = MainActivity.termuxCommand(ONLY_IF_ASKED + MainActivity.SCRIPT);
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(run);
            } else {
                context.startService(run);
            }
            Log.i("AIODOS", "Démarrage du téléphone : nœud AIODOS demandé à Termux");
        } catch (RuntimeException e) {
            // Termux absent, autorisation retirée, ou démarrage refusé : l'appli le dira
            // à sa prochaine ouverture
            Log.w("AIODOS", "Démarrage du téléphone : Termux n'a pas pu être appelé", e);
        }
    }
}
