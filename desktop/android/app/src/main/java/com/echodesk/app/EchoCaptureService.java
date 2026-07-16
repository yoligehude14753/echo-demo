package com.echodesk.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.IBinder;

import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;
import androidx.core.content.ContextCompat;

/**
 * User-visible free-capture foreground service.
 *
 * After the user's first explicit microphone grant, this service owns the
 * continuous capture lifetime. Formal meetings are overlays and never stop
 * the microphone. Android does not allow microphone capture to auto-resume
 * after reboot/force-stop, so those boundaries only post a restore notice.
 */
public final class EchoCaptureService extends Service {
  public static final String ACTION_START = "com.echodesk.app.capture.START";
  public static final String ACTION_PAUSE = "com.echodesk.app.capture.PAUSE";
  public static final String ACTION_STOP_EXIT =
      "com.echodesk.app.capture.STOP_EXIT";
  private static final String CHANNEL_ID = "echodesk_free_capture";
  private static final int NOTIFICATION_ID = 3303;
  private static final int RESTORE_NOTIFICATION_ID = 3304;
  private static volatile boolean active = false;

  public static void start(Context context) {
    Intent intent = new Intent(context, EchoCaptureService.class);
    intent.setAction(ACTION_START);
    ContextCompat.startForegroundService(context, intent);
  }

  public static void pause(Context context) {
    Intent intent = new Intent(context, EchoCaptureService.class);
    intent.setAction(ACTION_PAUSE);
    context.startService(intent);
  }

  public static void stopAndExit(Context context) {
    EchoCaptureRuntime runtime = EchoCaptureRuntime.get(context);
    runtime.markFreeModeEnabled(false);
    runtime.setPaused(false);
    runtime.setFormalMode(false, "");
    runtime.clearSession();
    Intent intent = new Intent(context, EchoCaptureService.class);
    intent.setAction(ACTION_STOP_EXIT);
    context.startService(intent);
  }

  public static void stop(Context context) {
    active = false;
    context.stopService(new Intent(context, EchoCaptureService.class));
  }

  public static boolean isActive() {
    return active;
  }

  public static void markRecording(Context context, String source) {
    EchoCaptureRuntime runtime = EchoCaptureRuntime.get(context);
    updateNotification(
        context,
        runtime.isFormalMode()
            ? "正式会议中 · 麦克风持续收音"
            : "自由收音中 · 麦克风持续开启",
        source == null || source.isBlank()
            ? "EchoDesk 正在后台监听有效语音"
            : "输入：" + source
    );
  }

  public static void updateQueueState(
      Context context,
      int queuedChunks,
      boolean formalMode,
      boolean paused
  ) {
    if (!active) return;
    String title;
    if (paused) {
      title = "自由收音已暂停";
    } else if (formalMode) {
      title = "正式会议中 · 麦克风持续收音";
    } else {
      title = "自由收音中 · 麦克风持续开启";
    }
    String text =
        queuedChunks > 0
            ? "待联网同步 " + queuedChunks + " 个音频片段"
            : paused
                ? "返回 EchoDesk 可恢复自由收音"
                : "有声音时自动识别，静音不会上传";
    updateNotification(context, title, text);
  }

  public static void notifyRestoreRequired(Context context) {
    EchoCaptureRuntime runtime = EchoCaptureRuntime.get(context);
    if (!runtime.isFreeModeEnabled()) return;
    createChannel(context);
    NotificationManager manager =
        (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
    manager.notify(
        RESTORE_NOTIFICATION_ID,
        buildRestoreNotification(context)
    );
  }

  @Override
  public void onCreate() {
    super.onCreate();
    createChannel(this);
  }

  @Override
  public int onStartCommand(Intent intent, int flags, int startId) {
    String action = intent != null ? intent.getAction() : null;
    EchoCaptureRuntime runtime = EchoCaptureRuntime.get(this);
    if (ACTION_PAUSE.equals(action)) {
      runtime.setPaused(true);
      EchoAudioPlugin.pauseActiveCaptureFromService();
      active = true;
      updateQueueState(this, runtime.queuedCount(), runtime.isFormalMode(), true);
      return START_NOT_STICKY;
    }
    if (ACTION_STOP_EXIT.equals(action)) {
      EchoAudioPlugin.stopActiveCaptureFromService();
      active = false;
      ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
      stopSelf();
      return START_NOT_STICKY;
    }

    runtime.markFreeModeEnabled(true);
    runtime.setPaused(false);
    active = true;
    ServiceCompat.startForeground(
        this,
        NOTIFICATION_ID,
        buildCaptureNotification(
            this,
            "自由收音启动中",
            "EchoDesk 将在后台持续监听有效语音",
            false
        ),
        ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
    );
    NotificationManager manager =
        (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
    manager.cancel(RESTORE_NOTIFICATION_ID);
    runtime.requestDrain();
    return START_NOT_STICKY;
  }

  @Override
  public void onTaskRemoved(Intent rootIntent) {
    // The service and native AudioRecord remain alive after the task is swiped.
    super.onTaskRemoved(rootIntent);
  }

  @Override
  public void onDestroy() {
    active = false;
    EchoAudioPlugin.stopActiveCaptureFromService();
    super.onDestroy();
  }

  @Nullable
  @Override
  public IBinder onBind(Intent intent) {
    return null;
  }

  private static void updateNotification(
      Context context,
      String title,
      String text
  ) {
    if (!active) return;
    createChannel(context);
    NotificationManager manager =
        (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
    manager.notify(
        NOTIFICATION_ID,
        buildCaptureNotification(context, title, text, title.contains("暂停"))
    );
  }

  private static void createChannel(Context context) {
    NotificationManager manager =
        (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
    NotificationChannel channel = new NotificationChannel(
        CHANNEL_ID,
        "EchoDesk 自由收音",
        NotificationManager.IMPORTANCE_LOW
    );
    channel.setDescription("显示自由收音、正式会议和离线队列状态");
    channel.setShowBadge(false);
    manager.createNotificationChannel(channel);
  }

  private static Notification buildCaptureNotification(
      Context context,
      String title,
      String text,
      boolean paused
  ) {
    PendingIntent openPending = openAppPendingIntent(context, 33031, false);
    Intent pauseIntent = new Intent(context, EchoCaptureService.class);
    pauseIntent.setAction(ACTION_PAUSE);
    PendingIntent pausePending = PendingIntent.getService(
        context,
        33032,
        pauseIntent,
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
    );
    Intent stopIntent = new Intent(context, EchoCaptureService.class);
    stopIntent.setAction(ACTION_STOP_EXIT);
    PendingIntent stopPending = PendingIntent.getService(
        context,
        33033,
        stopIntent,
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
    );

    NotificationCompat.Builder builder =
        new NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(title)
            .setContentText(text)
            .setContentIntent(openPending)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .addAction(0, "返回 EchoDesk", openPending);
    if (!paused) {
      builder.addAction(0, "暂停自由收音", pausePending);
    }
    builder.addAction(0, "停止并退出", stopPending);
    return builder.build();
  }

  private static Notification buildRestoreNotification(Context context) {
    PendingIntent openPending = openAppPendingIntent(context, 33041, true);
    return new NotificationCompat.Builder(context, CHANNEL_ID)
        .setSmallIcon(R.mipmap.ic_launcher)
        .setContentTitle("恢复 EchoDesk 自由收音")
        .setContentText("Android 重启后需要你打开一次 App 才能重新启用麦克风")
        .setContentIntent(openPending)
        .setAutoCancel(true)
        .setCategory(NotificationCompat.CATEGORY_REMINDER)
        .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
        .addAction(0, "打开并恢复", openPending)
        .build();
  }

  private static PendingIntent openAppPendingIntent(
      Context context,
      int requestCode,
      boolean restore
  ) {
    Intent openIntent = new Intent(context, MainActivity.class);
    openIntent.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
    if (restore) {
      openIntent.putExtra("echodesk_restore_free_capture", true);
    }
    return PendingIntent.getActivity(
        context,
        requestCode,
        openIntent,
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
    );
  }
}
