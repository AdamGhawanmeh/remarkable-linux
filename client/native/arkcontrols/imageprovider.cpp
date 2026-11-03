// Asynchronous QML image providers whose images are rendered in Python, without Python
// ever running on Qt Quick's image reader thread.
//
// Qt calls QQuickAsyncImageProvider::requestImageResponse() and the response's
// textureFactory() on its reader thread, in places where that thread holds the pixmap
// reader's lock. Implemented in Python, those calls wait for the GIL there; meanwhile
// the GUI thread, holding the GIL (any Python code that changes a QML property), can
// block on the same lock in QQuickPixmap::load(): a deadlock. So both are C++ here:
//
//   reader thread   requestImageResponse(id, size) creates a Response with a serial
//                   number and queues dispatcher.request(serial, id, size) to the GUI
//                   thread (posting an event takes no GIL)
//   Python          renders on its own threads, then calls rm_deliver_image(serial,
//                   QImage*) through ctypes (which releases the GIL)
//   any thread      the Response stores a copy of the image and queues finished() to
//                   its own (reader) thread, so it is never emitted before Qt connects
//
// Responses the engine has already deleted (cancelled requests) are skipped: deliveries
// look the serial up under the registry lock that the destructor also takes.

#include <QtCore/QHash>
#include <QtCore/QMutex>
#include <QtCore/QPointer>
#include <QtCore/QSize>
#include <QtGui/QImage>
#include <QtQml/QQmlEngine>
#include <QtQuick/QQuickAsyncImageProvider>

namespace {

class Response;

QMutex registryLock;
QHash<int, Response *> registry;
int lastSerial = 0;

class Response : public QQuickImageResponse
{
public:
    Response()
    {
        QMutexLocker locker(&registryLock);
        do {
            serial = lastSerial = lastSerial == INT_MAX ? 1 : lastSerial + 1;
        } while (registry.contains(serial));
        registry.insert(serial, this);
    }

    ~Response() override
    {
        QMutexLocker locker(&registryLock);
        registry.remove(serial);
    }

    QQuickTextureFactory *textureFactory() const override
    {
        QMutexLocker locker(&imageLock);
        return image.isNull() ? nullptr : QQuickTextureFactory::textureFactoryForImage(image);
    }

    // Called with registryLock held, so the response cannot be deleted meanwhile.
    void deliver(const QImage &result)
    {
        {
            QMutexLocker locker(&imageLock);
            image = result;
        }
        QMetaObject::invokeMethod(this, [this] { emit finished(); }, Qt::QueuedConnection);
    }

    int serial = 0;

private:
    mutable QMutex imageLock;
    QImage image;
};

class Provider : public QQuickAsyncImageProvider
{
public:
    explicit Provider(QObject *dispatcher) : dispatcher(dispatcher) {}

    QQuickImageResponse *requestImageResponse(const QString &id, const QSize &requestedSize) override
    {
        auto *response = new Response;
        const int serial = response->serial;
        bool queued = false;
        if (QObject *target = dispatcher.data()) {
            queued = QMetaObject::invokeMethod(target, "request", Qt::QueuedConnection, Q_ARG(int, serial),
                                               Q_ARG(QString, id), Q_ARG(QSize, requestedSize));
        }
        if (!queued) {          // the Python side is gone (shutting down): no image
            QMutexLocker locker(&registryLock);
            response->deliver(QImage());
        }
        return response;
    }

private:
    QPointer<QObject> dispatcher;
};

} // namespace

// Adds image://<name>/ to `engine` (a QQmlEngine*), answered by `dispatcher` (a QObject* on
// the GUI thread with a slot request(int serial, QString id, QSize requestedSize)). The
// engine owns the provider.
extern "C" Q_DECL_EXPORT bool rm_add_image_provider(void *engine, const char *name, void *dispatcher)
{
    auto *qmlEngine = qobject_cast<QQmlEngine *>(static_cast<QObject *>(engine));
    if (!qmlEngine || !dispatcher)
        return false;
    qmlEngine->addImageProvider(QString::fromUtf8(name), new Provider(static_cast<QObject *>(dispatcher)));
    return true;
}

// Completes request `serial` with a copy of *image (a QImage*; null for no image).
// Returns false if the engine no longer waits for it.
extern "C" Q_DECL_EXPORT bool rm_deliver_image(int serial, const void *image)
{
    const QImage result = image ? *static_cast<const QImage *>(image) : QImage();
    QMutexLocker locker(&registryLock);
    Response *response = registry.value(serial);
    if (!response)
        return false;
    response->deliver(result);
    return true;
}
