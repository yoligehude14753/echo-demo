package com.echodesk.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

public class BootReceiver extends BroadcastReceiver {
  private static final String TAG = "EchoDeskBoot";

  @Override
  public void onReceive(Context context, Intent intent) {
    String action = intent != null ? intent.getAction() : "";
    if (!Intent.ACTION_BOOT_COMPLETED.equals(action)
        && !"android.intent.action.QUICKBOOT_POWERON".equals(action)
        && !"com.htc.intent.action.QUICKBOOT_POWERON".equals(action)) {
      return;
    }

    try {
      Intent launchIntent = new Intent(context, MainActivity.class);
      launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
      launchIntent.putExtra("echodesk_boot_autostart", true);
      context.startActivity(launchIntent);
      Log.i(TAG, "EchoDesk launched after TV boot: " + action);
    } catch (Exception e) {
      Log.w(TAG, "Failed to launch EchoDesk after TV boot", e);
    }
  }
}
