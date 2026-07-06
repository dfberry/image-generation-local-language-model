// Reads the image currently running on the container app so a re-provision
// doesn't clobber the deployed revision. This lives in its OWN module (separate
// deployment scope) on purpose: declaring the `existing` container app in the
// same module that deploys it makes ARM see the resource depending on itself
// ("Circular dependency detected"). Isolating the read here breaks that cycle.
// This is the azd-standard fetch-latest-image pattern.

param exists bool
param name string

resource existingApp 'Microsoft.App/containerApps@2023-05-01' existing = if (exists) {
  name: name
}

// Empty string on first provision (exists=false) or if the running app has no
// containers yet; callers fall back to the placeholder image in that case.
output image string = exists ? (existingApp!.properties.template.containers[0].image ?? '') : ''
