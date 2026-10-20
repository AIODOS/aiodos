// L'appli Android d'AIODOS (docs/android.md) : une activité (l'interface du nœud), un
// service au premier plan (le nœud Python, par Chaquopy) et le démarrage avec le téléphone.
//
// tools/build_android.py prépare le dossier « aiodosStage » (build/android/ par défaut) :
//   python/          le code d'AIODOS et le logo, que Chaquopy met dans l'appli ;
//   jniLibs/         les programmes (llama.cpp, compagnon) rangés en bibliothèques ;
//   res/             les icônes tirées du logo ;
//   requirements-android.txt, wheels/   les paquets Python (pydantic-core compilé ici).
// Il donne aussi la version d'AIODOS, la clé de signature et le Python 3.13 du PC.
plugins {
    id("com.android.application") version "8.9.1"
    id("com.chaquo.python") version "17.1.0"
}

fun option(name: String, default: String): String =
    providers.gradleProperty(name).orNull ?: default

val stage = file(option("aiodosStage", "$rootDir/../build/android"))

android {
    namespace = "fr.arkane.aiodos"
    compileSdk = 35
    ndkVersion = option("aiodosNdk", "28.2.13676358")      // pour alléger les bibliothèques

    defaultConfig {
        applicationId = "fr.arkane.aiodos"
        minSdk = 28             // les programmes officiels de llama.cpp pour Android
        targetSdk = 34
        versionCode = option("aiodosVersionCode", "1").toInt()
        versionName = option("aiodosVersionName", "dev")
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    sourceSets {
        getByName("main") {
            manifest.srcFile("AndroidManifest.xml")
            java.setSrcDirs(listOf("src"))
            res.setSrcDirs(listOf("res", "$stage/res"))
            jniLibs.setSrcDirs(listOf("$stage/jniLibs"))
        }
    }

    signingConfigs {
        create("aiodos") {
            // Clé de développement gardée hors du dépôt : la même d'une version à l'autre,
            // sans quoi Android refuse la mise à jour de l'appli
            storeFile = file(option("aiodosKeystore",
                "${System.getProperty("user.home")}/.aiodos/android/aiodos-debug.keystore"))
            storePassword = "android"
            keyAlias = "aiodos"
            keyPassword = "android"
        }
    }

    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("aiodos")
        }
    }

    packaging {
        // Dépliées à l'installation : les programmes rangés en bibliothèques s'exécutent
        // depuis le dossier des bibliothèques de l'appli, le seul permis par Android
        jniLibs { useLegacyPackaging = true }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint { checkReleaseBuilds = false }
}

chaquopy {
    defaultConfig {
        version = "3.13"        // celle des paquets Android de Chaquopy (numpy, cryptography…)
        option("aiodosBuildPython", "").takeIf { it.isNotEmpty() }?.let { buildPython(it) }
        pip {
            install("-r", "$stage/requirements-android.txt")
        }
    }
    sourceSets {
        getByName("main") { setSrcDirs(listOf("$stage/python")) }
    }
}
