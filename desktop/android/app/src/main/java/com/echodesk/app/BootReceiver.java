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
      // Android forbids silently re-enabling microphone capture after a reboot.
      // Preserve the user's free-mode preference, but require an explicit tap.
      EchoCaptureService.stop(context);
      EchoCaptureService.notifyRestoreRequired(context);
      Log.i(TAG, "EchoDesk posted capture restore notice after boot: " + action);
    } catch (Exception e) {
      Log.w(TAG, "Failed to post EchoDesk capture restore notice", e);
    }
  }
}
