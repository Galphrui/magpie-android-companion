plugins {
    alias(libs.plugins.android.application)
}

android {
    namespace = "com.ruiao.magpie"
    // Match platforms;android-36 installed by tools/setup_mac_android.sh.
    compileSdk = 36

    defaultConfig {
        applicationId = "com.ruiao.magpie.companion.galph"
        minSdk = 27
        targetSdk = 36
        versionCode = 3
        versionName = "1.2"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        create("galphRelease") {
            val releaseStore = rootProject.file("signing/magpie-galph-release.p12")
            if (releaseStore.exists()) {
                storeFile = releaseStore
                storeType = "PKCS12"
                keyAlias = "magpie-galph-release"
                // Supply through the host terminal/CI, never commit the password.
                storePassword = providers.environmentVariable("MAGPIE_SIGNING_PASSWORD").orNull
                keyPassword = providers.environmentVariable("MAGPIE_SIGNING_PASSWORD").orNull
            }
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("galphRelease")
            optimization {
                enable = false
            }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
}

dependencies {
    implementation(libs.androidx.appcompat)
    implementation(libs.androidx.core.ktx)
    implementation(libs.material)
    testImplementation(libs.junit)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(libs.androidx.junit)
}
