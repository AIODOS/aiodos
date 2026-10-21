package fr.arkane.aiodos;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * Le nœud AIODOS du téléphone : Python (Chaquopy) dans le processus de l'appli, gardé en
 * marche par ce service au premier plan. Android exige sa notification pour tout travail
 * en arrière-plan ; elle est discrète (importance basse) et son bouton « Arrêter » arrête
 * le nœud. Appli fermée, le nœud continue : c'est lui qui calcule pour les autres machines.
 *
 * Le nœud lui-même : aiodos/node/android_app.py (start, stop). Ses programmes (llama.cpp,
 * le compagnon Internet) sont rangés parmi les bibliothèques de l'appli (nativeLibraryDir),
 * le seul dossier d'où Android permet d'exécuter un programme.
 *
 * Écran éteint, deux verrous : un verrou multicast tant que le nœud tourne (sans lui, le
 * Wi-Fi jette les annonces des machines du réseau local), et un verrou de veille seulement
 * en charge (le téléphone ne calcule qu'en charge, aiodos/network/worker.py).
 *
 * Les textes de la notification suivent la langue choisie dans les Réglages (Texts) : elle
 * change, la notification se réécrit dans la nouvelle.
 */
public class NodeService extends Service {
    static final String STOP = "fr.arkane.aiodos.ARRETER";
    private static final String CHANNEL = "noeud";
    private static final int NOTIFICATION = 1;
    private static final String TAG = "AIODOS";
    private static final long WATCH_EVERY = 30_000;      // ms

    /** L'adresse de l'interface, jeton compris, une fois le nœud prêt (MainActivity). */
    static volatile String url;
    /** Pourquoi le nœud n'a pas démarré, ou null. */
    static volatile String error;
    private static Thread starter;
    /** Le service en marche, pour réécrire sa notification quand la langue change. */
    private static volatile NodeService running;

    private PowerManager.WakeLock wake;
    private WifiManager.MulticastLock multicast;
    private BroadcastReceiver power;
    /** Le texte affiché par la notification : sa ressource, et son paramètre éventuel. */
    private volatile int shown = R.string.node_starting;
    private volatile String shownArg;

    /** Démarre le service (et le nœud) s'il ne tourne pas déjà. */
    static void start(Context context) {
        Intent intent = new Intent(context, NodeService.class);
        if (Build.VERSION.SDK_INT >= 26) {
            context.startForegroundService(intent);
        } else {
            context.startService(intent);
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        Device.init(this);
        Texts.init(this);
        running = this;
        channel();
    }

    /** Le canal de la notification, nommé dans la langue choisie (recréé, il est renommé). */
    private void channel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(CHANNEL,
                Texts.get(this, R.string.notification_channel), NotificationManager.IMPORTANCE_LOW);
            channel.setDescription(Texts.get(this, R.string.notification_channel_help));
            channel.setShowBadge(false);
            getSystemService(NotificationManager.class).createNotificationChannel(channel);
        }
    }

    /** La langue a changé (Texts.choose, appelé par le nœud) : canal et notification réécrits. */
    static void languageChanged() {
        NodeService service = running;
        if (service != null) {
            service.channel();
            service.update(service.shown, service.shownArg);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && STOP.equals(intent.getAction())) {
            stopNode();
            return START_NOT_STICKY;
        }
        String address = url;
        String failure = error;
        shown = address != null ? R.string.node_running
            : failure != null ? R.string.node_failed : R.string.node_starting;
        shownArg = address == null ? failure : null;
        if (!goForeground(text(shown, shownArg))) {
            stopSelf();                 // refusé par Android (relance en arrière-plan…)
            return START_NOT_STICKY;
        }
        holdLocks();
        startNode();
        return START_STICKY;            // arrêté par Android : il redémarre le nœud
    }

    private void startNode() {
        synchronized (NodeService.class) {
            if (starter != null) {
                return;                 // déjà démarré (ou en cours) dans ce processus
            }
            starter = new Thread(this::run, "aiodos-demarrage");
            starter.start();
        }
    }

    private void run() {
        try {
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(this));
            }
            PyObject address = node().callAttr("start", getFilesDir().getAbsolutePath(),
                getApplicationInfo().nativeLibraryDir, getCacheDir().getAbsolutePath());
            url = address.toString();
            error = null;
            update(R.string.node_running, null);
            Log.i(TAG, "Nœud AIODOS prêt");
        } catch (Throwable e) {
            Log.e(TAG, "Le nœud AIODOS n'a pas démarré", e);
            error = summary(e);
            update(R.string.node_failed, error);
            return;
        }
        watch();
    }

    /** Le nœud tourne-t-il toujours ? Sinon, la notification le dit (rouvrir l'appli le
     *  redémarre dans un processus neuf). */
    private void watch() {
        while (true) {
            try {
                Thread.sleep(WATCH_EVERY);
                if (!node().callAttr("alive").toBoolean()) {
                    error = Texts.get(this, R.string.node_stopped_reason);
                    url = null;
                    update(R.string.node_stopped, null);
                    return;
                }
            } catch (InterruptedException e) {
                return;
            } catch (Throwable e) {
                Log.w(TAG, "Surveillance du nœud AIODOS", e);
            }
        }
    }

    private static PyObject node() {
        return Python.getInstance().getModule("aiodos.node.android_app");
    }

    /** La première ligne utile d'une erreur Python (« NodeError: … »), sans la pile. */
    static String summary(Throwable e) {
        String text = String.valueOf(e.getMessage());
        for (String line : text.split("\n")) {
            if (!line.trim().isEmpty()) {
                return line.trim();
            }
        }
        return e.getClass().getSimpleName();
    }

    /** « Arrêter » : le nœud s'arrête proprement (compagnon, llama.cpp), le service aussi,
     *  puis le processus : Python ne redémarre pas dans un même processus, la prochaine
     *  ouverture de l'appli repart à neuf. */
    private void stopNode() {
        update(R.string.node_stopping, null);
        new Thread(() -> {
            try {
                if (Python.isStarted() && url != null) {
                    node().callAttr("stop");
                }
            } catch (Throwable e) {
                Log.w(TAG, "Arrêt du nœud AIODOS", e);
            }
            releaseLocks();
            stopForeground(true);
            stopSelf();
            android.os.Process.killProcess(android.os.Process.myPid());
        }, "aiodos-arret").start();
    }

    /** Passer au premier plan, comme Android l'exige d'un service démarré par
     *  startForegroundService ; faux s'il le refuse (relance en arrière-plan…), sans planter. */
    private boolean goForeground(String text) {
        try {
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTIFICATION, notification(text),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else {
                startForeground(NOTIFICATION, notification(text));
            }
            return true;
        } catch (RuntimeException e) {
            Log.w(TAG, "Service au premier plan refusé", e);
            return false;
        }
    }

    /** Le texte de la notification change ; le service reste au premier plan. */
    private void update(int id, String arg) {
        shown = id;
        shownArg = arg;
        try {
            getSystemService(NotificationManager.class).notify(NOTIFICATION,
                notification(text(id, arg)));
        } catch (RuntimeException e) {
            Log.w(TAG, "Notification du nœud", e);
        }
    }

    private String text(int id, String arg) {
        return arg == null ? Texts.get(this, id) : Texts.get(this, id, arg);
    }

    private Notification notification(String text) {
        Intent open = new Intent(this, MainActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent show = PendingIntent.getActivity(this, 0, open,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1,
            new Intent(this, NodeService.class).setAction(STOP),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
            ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        builder.setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("AIODOS")
            .setContentText(text)
            .setStyle(new Notification.BigTextStyle().bigText(text))
            .setContentIntent(show)
            .setOngoing(true)
            .setShowWhen(false)
            .addAction(new Notification.Action.Builder(null,
                Texts.get(this, R.string.notification_stop), stop).build());
        if (Build.VERSION.SDK_INT >= 31) {
            builder.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        }
        return builder.build();
    }

    private void holdLocks() {
        if (multicast == null) {
            WifiManager wifi = getApplicationContext().getSystemService(WifiManager.class);
            if (wifi != null) {
                multicast = wifi.createMulticastLock("aiodos:annonces");
                multicast.setReferenceCounted(false);
                multicast.acquire();
            }
        }
        if (power == null) {
            power = new BroadcastReceiver() {
                @Override
                public void onReceive(Context context, Intent intent) {
                    charging(Intent.ACTION_POWER_CONNECTED.equals(intent.getAction()));
                }
            };
            IntentFilter filter = new IntentFilter(Intent.ACTION_POWER_CONNECTED);
            filter.addAction(Intent.ACTION_POWER_DISCONNECTED);
            registerReceiver(power, filter);
            Intent battery = registerReceiver(null,
                new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
            charging(battery != null
                && battery.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0);
        }
    }

    /** En charge : le processeur reste éveillé, écran éteint, pour calculer. */
    private void charging(boolean plugged) {
        if (plugged && wake == null) {
            PowerManager manager = getSystemService(PowerManager.class);
            wake = manager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "aiodos:calcul");
            wake.setReferenceCounted(false);
            wake.acquire();
        } else if (!plugged && wake != null) {
            wake.release();
            wake = null;
        }
    }

    private void releaseLocks() {
        if (power != null) {
            try {
                unregisterReceiver(power);
            } catch (IllegalArgumentException ignored) {
                // déjà retiré
            }
            power = null;
        }
        charging(false);
        if (multicast != null) {
            multicast.release();
            multicast = null;
        }
    }

    @Override
    public void onDestroy() {
        if (running == this) {
            running = null;
        }
        releaseLocks();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
