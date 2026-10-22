package com.voiceflock.app

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import java.nio.FloatBuffer

/** ECAPA-TDNN speaker encoder (with feature extraction inside the graph) via ONNX Runtime. */
class Embedder(modelPath: String) : AutoCloseable {
    private val env: OrtEnvironment = OrtEnvironment.getEnvironment()
    private val session: OrtSession

    init {
        val opts = OrtSession.SessionOptions()
        opts.setIntraOpNumThreads(2)
        opts.setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT)
        session = env.createSession(modelPath, opts)
    }

    /** wav: float32 samples at 16 kHz in [-1, 1]. Returns an L2-normalised 192-d embedding. */
    fun embed(wav: FloatArray): FloatArray {
        OnnxTensor.createTensor(env, FloatBuffer.wrap(wav), longArrayOf(1, wav.size.toLong())).use { t ->
            session.run(mapOf("wav" to t)).use { r ->
                @Suppress("UNCHECKED_CAST")
                val out = r[0].value as Array<FloatArray>
                return Gallery.normalise(out[0])
            }
        }
    }

    override fun close() { session.close() }
}
