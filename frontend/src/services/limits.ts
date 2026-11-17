/**
 * Length limits on free-text inputs (M56). The API enforces the same limits
 * (src/pointcloud_detect/api/models.py); setting them on the inputs tells the user
 * before a request fails.
 */
export const MAX_NAME_LENGTH = 128;
export const MAX_CLASS_NAME_LENGTH = 64;
export const MAX_CLASSES = 100;
/** A comma-separated class list: MAX_CLASSES names and their separators. */
export const MAX_CLASS_LIST_LENGTH = MAX_CLASSES * (MAX_CLASS_NAME_LENGTH + 2);
/** TrainingRequest.epochs upper bound (api/routes/training.py). */
export const MAX_EPOCHS = 1000;
