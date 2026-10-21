package fr.arkane.aiodos;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.BatteryManager;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.net.Inet4Address;

/**
 * Ce que le nœud Python demande au téléphone (aiodos/node/android_app.py), sans permission
 * particulière : la batterie et le Wi-Fi. Avant l'appli, Termux:API le donnait.
 */
public final class Device {
    private static volatile Context context;

    private Device() {
    }

    static void init(Context any) {
        context = any.getApplicationContext();
    }

    /** La batterie, au format de termux-battery-status que lit aiodos/node/power.py :
     *  {"percentage", "plugged", "status", "temperature"} ; "" si illisible. */
    public static String battery() {
        Context app = context;
        Intent state = app == null ? null
            : app.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (state == null) {
            return "";
        }
        int level = state.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
        int scale = state.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
        int plugged = state.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1);
        int status = state.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
        int tenths = state.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Integer.MIN_VALUE);
        JSONObject out = new JSONObject();
        try {
            if (level >= 0 && scale > 0) {
                out.put("percentage", Math.round(level * 100f / scale));
            }
            if (plugged >= 0) {
                out.put("plugged", plugged == 0 ? "UNPLUGGED"
                    : plugged == BatteryManager.BATTERY_PLUGGED_AC ? "PLUGGED_AC"
                    : plugged == BatteryManager.BATTERY_PLUGGED_USB ? "PLUGGED_USB"
                    : plugged == BatteryManager.BATTERY_PLUGGED_WIRELESS ? "PLUGGED_WIRELESS"
                    : "PLUGGED");
            }
            out.put("status", status == BatteryManager.BATTERY_STATUS_CHARGING ? "CHARGING"
                : status == BatteryManager.BATTERY_STATUS_DISCHARGING ? "DISCHARGING"
                : status == BatteryManager.BATTERY_STATUS_FULL ? "FULL"
                : status == BatteryManager.BATTERY_STATUS_NOT_CHARGING ? "NOT_CHARGING"
                : "UNKNOWN");
            if (tenths != Integer.MIN_VALUE) {
                out.put("temperature", tenths / 10.0);
            }
        } catch (JSONException e) {
            return "";
        }
        return out.toString();
    }

    /** Les adresses IPv4 du Wi-Fi et de l'Ethernet, sans celles d'un VPN (NordVPN…) :
     *  [{"name", "address", "prefix"}] en JSON, lu par aiodos/network/interfaces.py pour
     *  annoncer le nœud sur le vrai réseau local ; "[]" si rien n'est lisible. */
    public static String lanAddresses() {
        Context app = context;
        ConnectivityManager manager = app == null ? null
            : app.getSystemService(ConnectivityManager.class);
        JSONArray out = new JSONArray();
        if (manager == null) {
            return out.toString();
        }
        for (Network network : manager.getAllNetworks()) {
            NetworkCapabilities capabilities = manager.getNetworkCapabilities(network);
            LinkProperties link = manager.getLinkProperties(network);
            if (capabilities == null || link == null
                    || capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
                    || !(capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                        || capabilities.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET))) {
                continue;
            }
            for (LinkAddress address : link.getLinkAddresses()) {
                if (!(address.getAddress() instanceof Inet4Address)) {
                    continue;
                }
                try {
                    out.put(new JSONObject()
                        .put("name", String.valueOf(link.getInterfaceName()))
                        .put("address", address.getAddress().getHostAddress())
                        .put("prefix", address.getPrefixLength()));
                } catch (JSONException ignored) {
                    // adresse suivante
                }
            }
        }
        return out.toString();
    }

    /** 1 : relié à un Wi-Fi ; 0 : non (données mobiles, ou rien) ; -1 : on ne sait pas. */
    public static int wifi() {
        Context app = context;
        ConnectivityManager manager = app == null ? null
            : app.getSystemService(ConnectivityManager.class);
        if (manager == null) {
            return -1;
        }
        Network active = manager.getActiveNetwork();
        if (active == null) {
            return 0;
        }
        NetworkCapabilities capabilities = manager.getNetworkCapabilities(active);
        if (capabilities == null) {
            return -1;
        }
        return capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ? 1 : 0;
    }
}
