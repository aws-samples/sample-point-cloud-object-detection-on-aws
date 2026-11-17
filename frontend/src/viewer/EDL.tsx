/**
 * Eye-Dome Lighting (EDL) post-process for point clouds.
 *
 * Screen-space technique (CloudCompare / Potree style): darkens each pixel
 * based on how much farther its screen-space neighbors are, which makes depth
 * discontinuities read as crisp outlines — giving an unshaded point cloud a
 * strong sense of 3D structure without needing normals.
 *
 * Implemented as a manual render-to-target + full-screen shader so it works
 * with the plain `three` build (no extra postprocessing dependency). Taking a
 * `useFrame` priority > 0 disables R3F's automatic render and hands the loop
 * to us.
 */
import { useMemo } from 'react';
import { useThree, useFrame } from '@react-three/fiber';
import * as THREE from 'three';

const EDL_FRAGMENT = /* glsl */ `
  #include <packing>
  uniform sampler2D tDiffuse;
  uniform sampler2D tDepth;
  uniform float cameraNear;
  uniform float cameraFar;
  uniform vec2 resolution;
  uniform float edlStrength;
  uniform float edlRadius;
  varying vec2 vUv;

  float linearDepth(vec2 uv) {
    float fragZ = texture2D(tDepth, uv).x;
    float viewZ = perspectiveDepthToViewZ(fragZ, cameraNear, cameraFar);
    return -viewZ; // positive distance from camera
  }

  void main() {
    vec4 color = texture2D(tDiffuse, vUv);
    float centerRaw = texture2D(tDepth, vUv).x;

    // Background pixel — leave untouched.
    if (centerRaw >= 1.0 || edlStrength <= 0.0) {
      gl_FragColor = color;
      return;
    }

    float d = linearDepth(vUv);
    float logD = log2(d + 1e-6);
    vec2 texel = edlRadius / resolution;

    vec2 offsets[4];
    offsets[0] = vec2(1.0, 0.0);
    offsets[1] = vec2(-1.0, 0.0);
    offsets[2] = vec2(0.0, 1.0);
    offsets[3] = vec2(0.0, -1.0);

    float response = 0.0;
    for (int i = 0; i < 4; i++) {
      vec2 uv = vUv + offsets[i] * texel;
      float nRaw = texture2D(tDepth, uv).x;
      // Background neighbor => treat as same depth (no darkening from the void).
      float nd = (nRaw >= 1.0) ? d : linearDepth(uv);
      float nLog = log2(nd + 1e-6);
      response += max(0.0, nLog - logD);
    }
    response *= 0.25;

    float shade = exp(-response * edlStrength * 300.0);
    gl_FragColor = vec4(color.rgb * shade, color.a);
  }
`;

const EDL_VERTEX = /* glsl */ `
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

export function EDL({ strength, radius = 1.4 }: { strength: number; radius?: number }) {
  const { gl, scene, camera, size } = useThree();

  const { target, quadScene, quadCamera, material } = useMemo(() => {
    const target = new THREE.WebGLRenderTarget(1, 1, {
      minFilter: THREE.NearestFilter,
      magFilter: THREE.NearestFilter,
    });
    target.texture.colorSpace = THREE.SRGBColorSpace;
    target.depthTexture = new THREE.DepthTexture(1, 1);
    target.depthTexture.type = THREE.UnsignedIntType;

    const material = new THREE.ShaderMaterial({
      vertexShader: EDL_VERTEX,
      fragmentShader: EDL_FRAGMENT,
      uniforms: {
        tDiffuse: { value: target.texture },
        tDepth: { value: target.depthTexture },
        cameraNear: { value: 0.1 },
        cameraFar: { value: 1000 },
        resolution: { value: new THREE.Vector2(1, 1) },
        edlStrength: { value: strength },
        edlRadius: { value: radius },
      },
      depthTest: false,
      depthWrite: false,
    });

    const quadScene = new THREE.Scene();
    const quadCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    const quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), material);
    quad.frustumCulled = false;
    quadScene.add(quad);

    return { target, quadScene, quadCamera, material };
  }, [radius, strength]);

  useFrame(() => {
    const dpr = gl.getPixelRatio();
    const w = Math.max(1, Math.floor(size.width * dpr));
    const h = Math.max(1, Math.floor(size.height * dpr));

    if (target.width !== w || target.height !== h) {
      target.setSize(w, h);
      material.uniforms.resolution.value.set(w, h);
    }

    const cam = camera as THREE.PerspectiveCamera;
    material.uniforms.cameraNear.value = cam.near;
    material.uniforms.cameraFar.value = cam.far;
    material.uniforms.edlStrength.value = strength;
    material.uniforms.edlRadius.value = radius;

    gl.setRenderTarget(target);
    gl.clear();
    gl.render(scene, camera);

    gl.setRenderTarget(null);
    gl.clear();
    gl.render(quadScene, quadCamera);
  }, 1);

  return null;
}
