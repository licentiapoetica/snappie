// KDE Share menu entry "Upload with Snappie" (a KF6 Purpose export plugin).
// It only hands the files to Snappie over D-Bus: Snappie does the upload, shows its progress
// and answers with the links. D-Bus starts Snappie if it isn't running.
#include <KPluginFactory>
#include <Purpose/Job>
#include <Purpose/PluginBase>

#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusPendingCallWatcher>
#include <QDBusPendingReply>
#include <QJsonArray>
#include <QUrl>

class SnappieJob : public Purpose::Job
{
    Q_OBJECT
public:
    using Purpose::Job::Job;

    void start() override
    {
        QStringList files;
        const QJsonArray urls = data().value(QStringLiteral("urls")).toArray();
        for (const QJsonValue &v : urls) {
            const QUrl url(v.toString());
            if (url.isLocalFile())
                files << url.toLocalFile();
        }
        if (files.isEmpty()) {
            setError(UserDefinedError);
            setErrorText(QStringLiteral("Snappie can only upload local files"));
            emitResult();
            return;
        }
        QDBusMessage call = QDBusMessage::createMethodCall(QStringLiteral("io.github.snappie"), QStringLiteral("/"),
                                                           QStringLiteral("io.github.snappie"),
                                                           QStringLiteral("upload"));
        call << files;
        // big videos on slow connections take a while; Snappie shows the progress meanwhile
        const int timeout = 6 * 60 * 60 * 1000;
        auto *watcher = new QDBusPendingCallWatcher(QDBusConnection::sessionBus().asyncCall(call, timeout), this);
        connect(watcher, &QDBusPendingCallWatcher::finished, this, [this](QDBusPendingCallWatcher *w) {
            QDBusPendingReply<QStringList> reply = *w;
            w->deleteLater();
            if (reply.isError()) {
                setError(UserDefinedError);
                setErrorText(reply.error().message());
            } else if (reply.value().size() == 1) {
                // the app shows / copies the link, like with its other upload plugins
                setOutput({{QStringLiteral("url"), reply.value().first()}});
            }
            emitResult();
        });
    }
};

class SnappiePlugin : public Purpose::PluginBase
{
    Q_OBJECT
public:
    using Purpose::PluginBase::PluginBase;

    Purpose::Job *createJob() const override
    {
        return new SnappieJob(nullptr);
    }
};

K_PLUGIN_CLASS_WITH_JSON(SnappiePlugin, "snappie_purpose.json")

#include "snappie_purpose.moc"
