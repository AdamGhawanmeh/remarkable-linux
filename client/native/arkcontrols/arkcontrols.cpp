// The two C++ pieces of the official "ark.controls" QML module that QML cannot express
// and PySide6 cannot register: the arkImageInfo value type and its provider singleton,
// as used by ark-imports/ark/controls/Icon.qml:
//
//     property arkImageInfo _info: ArkImageDataProvider.resolveImageInfo(
//             root.source, blackAndWhite, root.borderColor, root.fill)
//     ... sourceSize: _info.size; Image { source: _info.source }
//
// resolveImageInfo() maps an icon (an SVG in qrc:/ark/icons/) to an
// image://arkicon/... URL, which the client's Python image provider renders in the
// requested colours, and reports the SVG's own size. Loaded with ctypes into the
// running Qt; call rm_register_ark_controls() before the QML engine loads anything.

#include <QtCore/QFile>
#include <QtCore/QRegularExpression>
#include <QtCore/QSize>
#include <QtCore/QUrl>
#include <QtCore/QUrlQuery>
#include <QtCore/QVariant>
#include <QtQml/QQmlEngine>
#include <QtQml/qqml.h>

class ArkImageInfo
{
    Q_GADGET
    QML_VALUE_TYPE(arkImageInfo)
    Q_PROPERTY(QUrl source MEMBER source)
    Q_PROPERTY(QSize size MEMBER size)
public:
    QUrl source;
    QSize size;
    bool operator==(const ArkImageInfo &other) const { return source == other.source && size == other.size; }
};

class ArkImageDataProvider : public QObject
{
    Q_OBJECT
    QML_NAMED_ELEMENT(ArkImageDataProvider)
    QML_SINGLETON
public:
    using QObject::QObject;

    // Colours arrive as QVariant(QColor): QtGui is loaded in the running app, and its
    // registered conversion gives the "#aarrggbb"/"#rrggbb" name, so no QtGui link here.
    Q_INVOKABLE ArkImageInfo resolveImageInfo(const QString &source, bool blackAndWhite,
                                              const QVariant &borderColor, const QVariant &fill) const
    {
        ArkImageInfo info;
        if (source.isEmpty())
            return info;
        QString path = source;
        if (path.startsWith(QLatin1String("qrc:")))
            path = path.mid(3);                       // "qrc:/ark/icons/x" -> ":/ark/icons/x"
        info.size = svgSize(path);
        QUrl url(QStringLiteral("image://arkicon/") + QString::fromLatin1(QUrl::toPercentEncoding(source)));
        QUrlQuery query;
        query.addQueryItem(QStringLiteral("border"), colorName(borderColor));
        query.addQueryItem(QStringLiteral("fill"), colorName(fill));
        if (blackAndWhite)
            query.addQueryItem(QStringLiteral("bw"), QStringLiteral("1"));
        url.setQuery(query);
        info.source = url;
        return info;
    }

private:
    static QString colorName(const QVariant &color)
    {
        return color.isValid() ? color.toString() : QString();
    }

    static QSize svgSize(const QString &path)
    {
        QFile file(path);
        if (!file.open(QIODevice::ReadOnly))
            return {};
        const QString head = QString::fromUtf8(file.read(512));
        static const QRegularExpression width(QStringLiteral("width=\"([0-9.]+)"));
        static const QRegularExpression height(QStringLiteral("height=\"([0-9.]+)"));
        const auto w = width.match(head), h = height.match(head);
        if (!w.hasMatch() || !h.hasMatch())
            return {};
        return QSize(qRound(w.captured(1).toDouble()), qRound(h.captured(1).toDouble()));
    }
};

extern "C" Q_DECL_EXPORT void rm_register_ark_controls()
{
    qmlRegisterTypesAndRevisions<ArkImageInfo>("ark.controls", 1);
    qmlRegisterTypesAndRevisions<ArkImageDataProvider>("ark.controls", 1);
}

#include "arkcontrols.moc"
