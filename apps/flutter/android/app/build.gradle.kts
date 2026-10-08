plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

val brandAppIdRaw =
    (findProperty("brandAppId") as? String)?.trim().orEmpty()
val allowDefaultBrand =
    (findProperty("allowDefaultBrand") as? String)?.trim() == "1" ||
        System.getenv("ALLOW_DEFAULT_BRAND") == "1"
val brandAppId = brandAppIdRaw.ifBlank { "app.luma.client" }
val brandDisplayName =
    (findProperty("brandDisplayName") as? String)?.takeIf { it.isNotBlank() }
        ?: "Luma"

gradle.taskGraph.whenReady {
    val releasing = allTasks.any { task ->
        val name = task.name
        name.contains("Release") && !name.contains("Test")
    }
    if (!releasing || allowDefaultBrand) return@whenReady
    if (brandAppIdRaw.isBlank()) {
        throw GradleException(
            "发布构建必须设置 -PbrandAppId。若要使用开源默认 app.luma.client，请设置 -PallowDefaultBrand=1 或 ALLOW_DEFAULT_BRAND=1。",
        )
    }
    if (brandAppIdRaw == "app.luma.client") {
        throw GradleException(
            "brandAppId 不能使用开源默认 app.luma.client，除非设置 -PallowDefaultBrand=1 或 ALLOW_DEFAULT_BRAND=1。",
        )
    }
}

android {
    namespace = "com.example.luma_client"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = brandAppId
        resValue("string", "app_name", brandDisplayName)
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Uses the version code from pubspec.yaml. When using split APKs, 1000 * ABI_VERSION
        // is added automatically by Flutter. (https://developer.android.com/studio/build/configure-apk-splits#configure-APK-versions)
        // You can force using the value of versionCode by specifying the `-P force-version-code-ignoring-abi=true`
        // flag during build.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
