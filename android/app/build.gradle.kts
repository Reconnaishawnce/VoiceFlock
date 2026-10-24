plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// CI passes -PversionCode=<run number> -PversionName=<x.y.z>; local builds use the defaults below.
val vCode = (project.findProperty("versionCode") as String?)?.toIntOrNull() ?: 1
val vName = (project.findProperty("versionName") as String?) ?: "0.2.0"
// Release signing from the environment (GitHub secrets); absent -> the debug key, so local builds still work.
val ksPath = System.getenv("VOICEFLOCK_KEYSTORE")
val ksPass = System.getenv("VOICEFLOCK_KEYSTORE_PASSWORD")
val ksAlias = System.getenv("VOICEFLOCK_KEY_ALIAS")
val ksKeyPass = System.getenv("VOICEFLOCK_KEY_PASSWORD")

android {
    namespace = "com.voiceflock.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.voiceflock.app"
        minSdk = 26
        targetSdk = 34
        versionCode = vCode
        versionName = vName
    }
    signingConfigs {
        if (ksPath != null && ksPass != null && ksAlias != null && ksKeyPass != null) {
            create("upload") {
                storeFile = file(ksPath)
                storePassword = ksPass
                keyAlias = ksAlias
                keyPassword = ksKeyPass
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("upload") ?: signingConfigs.getByName("debug")
        }
    }
    // one APK per CPU type keeps the download ~25 MB instead of ~90 MB; the universal one is for Obtainium
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "armeabi-v7a", "x86_64")
            isUniversalApk = true
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true; buildConfig = true }
    packaging { resources.excludes += "/META-INF/{AL2.0,LGPL2.1}" }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.09.03")
    implementation(composeBom)
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.6")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.19.2")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
