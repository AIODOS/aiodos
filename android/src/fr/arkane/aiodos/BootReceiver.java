package fr.arkane.aiodos;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

import java.io.File;

/**
 * Démarrage du téléphone : le nœud AIODOS démarre en arrière-plan, sans ouvrir l'appli, si
 * « Démarrer avec le téléphone » est activé (Cette machine ; fichier « autostart » de
 * l'appli, aiodos/node/autostart.py). Android permet de démarrer un service au premier plan
 * juste après le démarrage.
 */
public class BootReceiver extends BroadcastReceiver {
    static final String FLAG = "autostart";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action)
                && !"android.intent.action.QUICKBOOT_POWERON".equals(action)) {
            return;
        }
        if (!new File(context.getFilesDir(), FLAG).isFile()) {
            return;
        }
        try {
            NodeService.start(context);
            Log.i("AIODOS", "Démarrage du téléphone : nœud AIODOS démarré");
        } catch (RuntimeException e) {
            // démarrage refusé par Android : l'appli le démarrera à sa prochaine ouverture
            Log.w("AIODOS", "Démarrage du téléphone : nœud AIODOS non démarré", e);
        }
    }
}
