package fr.arkane.aiodos;

import android.content.Context;
import android.content.SharedPreferences;
import android.content.res.Configuration;

import java.util.Locale;

/**
 * La langue des textes de l'appli (notification, écran d'attente, liens) : celle choisie dans
 * les Réglages d'AIODOS, sinon celle du téléphone. Les textes : res/values (l'anglais, langue
 * de repli d'un téléphone ni en français ni en anglais) et res/values-fr.
 *
 * Le nœud (Python) la donne au démarrage et à chaque changement du réglage
 * (aiodos/node/android_app.py) ; elle est gardée pour le démarrage suivant, où la
 * notification s'affiche avant que Python ne réponde.
 */
final class Texts {
    private static final String PREFS = "aiodos";
    private static final String KEY = "langue";
    private static volatile Context app;

    private Texts() {
    }

    static void init(Context context) {
        app = context.getApplicationContext();
    }

    /** Appelée par le nœud : « fr », « en », ou null (la langue du téléphone). */
    static void choose(String lang) {
        Context context = app;
        if (context == null) {
            return;
        }
        String before = chosen(context);
        if (lang == null ? before == null : lang.equals(before)) {
            return;
        }
        SharedPreferences.Editor editor = prefs(context).edit();
        if (lang == null) {
            editor.remove(KEY);
        } else {
            editor.putString(KEY, lang);
        }
        editor.apply();
        NodeService.languageChanged();
    }

    /** Un texte de l'appli dans la langue choisie, paramètres posés (%1$s…). */
    static String get(Context base, int id, Object... args) {
        Context context = of(base);
        return args.length == 0 ? context.getString(id) : context.getString(id, args);
    }

    /** Le contexte qui donne les textes dans la langue choisie (sinon celui du téléphone). */
    static Context of(Context base) {
        String lang = chosen(base);
        if (lang == null) {
            return base;
        }
        Configuration config = new Configuration(base.getResources().getConfiguration());
        config.setLocale(new Locale(lang));
        return base.createConfigurationContext(config);
    }

    private static String chosen(Context context) {
        String lang = prefs(context).getString(KEY, null);
        return "fr".equals(lang) || "en".equals(lang) ? lang : null;
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
