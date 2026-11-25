plugins {
    alias(libs.plugins.android.application)
}

abstract class UnpackMagpieWebAssets : DefaultTask() {
    @get:InputFile
    @get:PathSensitive(PathSensitivity.NONE)
    abstract val archive: RegularFileProperty

    @get:OutputDirectory
    abstract val assetsDirectory: DirectoryProperty

    @get:Inject
    abstract val archives: ArchiveOperations

    @get:Inject
    abstract val files: FileSystemOperations

    @TaskAction
    fun unpack() {
        files.sync {
            from(archives.zipTree(archive))
            into(assetsDirectory)
        }
    }
}

android {
    namespace = "com.ruiao.magpie"
    // Match platforms;android-36 installed by tools/setup_mac_android.sh.
    compileSdk = 36

    defaultConfig {
        applicationId = "com.ruiao.magpie.companion.galph"
        minSdk = 27
        targetSdk = 36
        versionCode = 6
        versionName = "1.5"

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
        debug {
            applicationIdSuffix = ".debug"
            if (providers.environmentVariable("MAGPIE_SIGNING_PASSWORD").isPresent) {
                signingConfig = signingConfigs.getByName("galphRelease")
            }
        }
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

androidComponents {
    onVariants { variant ->
        val unpack = tasks.register<UnpackMagpieWebAssets>(
            "unpack${variant.name.replaceFirstChar { it.uppercaseChar() }}MagpieWebAssets"
        ) {
            archive.set(layout.projectDirectory.file("web-assets.zip"))
        }
        variant.sources.assets?.addGeneratedSourceDirectory(unpack, UnpackMagpieWebAssets::assetsDirectory)
    }
}

dependencies {
    implementation(libs.androidx.appcompat)
    implementation(libs.androidx.core.ktx)
    implementation(libs.material)
    implementation(libs.zxing.embedded)
    testImplementation(libs.junit)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(libs.androidx.junit)
}
